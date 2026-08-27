package com.communism420.acbridge;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.RemoteException;
import android.system.Os;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

import rikka.shizuku.Shizuku;

/** DUMP-protected entry point for status, permission, and UserService execution. */
public final class ShizukuActivity extends Activity {
    private static final int REQUEST_PERMISSION_CODE = 43110;
    private static final long BINDER_WAIT_MILLIS = 8000L;
    private static final long USER_SERVICE_BIND_WAIT_MILLIS = 15000L;
    private static final long CANCELLATION_POLL_MILLIS = 100L;
    private static final long CANCELLATION_MARKER_RETENTION_MILLIS = 30000L;
    private static final Object ACTIVE_REQUESTS_LOCK = new Object();
    private static final Map<String, WeakReference<ShizukuActivity>> ACTIVE_REQUESTS =
            new HashMap<String, WeakReference<ShizukuActivity>>();
    private static final Handler PROCESS_HANDLER = new Handler(Looper.getMainLooper());

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean terminal = new AtomicBoolean(false);
    private TextView statusView;
    private String operation = "";
    private String requestId = "";
    private String permissionHostRequestId = "";
    private int timeoutSeconds = 60;
    private boolean passiveOperation = true;
    private boolean foregroundResumed;
    private boolean windowHasFocus;
    private boolean operationStarted;
    private boolean listenersRegistered;
    private boolean userServiceBound;
    private boolean activeRequestRegistered;
    private IShizukuCommandService commandService;
    private Shizuku.UserServiceArgs userServiceArgs;

    private final Runnable cancellationPoll = new Runnable() {
        @Override
        public void run() {
            if (terminal.get()) {
                return;
            }
            if (cancellationMarker().isFile()) {
                completeHostCancellation();
                return;
            }
            mainHandler.postDelayed(this, CANCELLATION_POLL_MILLIS);
        }
    };

    private final Shizuku.OnBinderReceivedListener binderReceivedListener =
            new Shizuku.OnBinderReceivedListener() {
                @Override
                public void onBinderReceived() {
                    handleAvailableBinder();
                }
            };

    private final Shizuku.OnBinderDeadListener binderDeadListener =
            new Shizuku.OnBinderDeadListener() {
                @Override
                public void onBinderDead() {
                    completeStatus("binder_dead", "Shizuku stopped or its binder connection was lost.");
                }
            };

    private final Shizuku.OnRequestPermissionResultListener permissionResultListener =
            new Shizuku.OnRequestPermissionResultListener() {
                @Override
                public void onRequestPermissionResult(int requestCode, int grantResult) {
                    if (requestCode != REQUEST_PERMISSION_CODE || terminal.get()) {
                        return;
                    }
                    if (grantResult == PackageManager.PERMISSION_GRANTED) {
                        completeStatus("permission_granted", "Shizuku access was granted to OpenADB Bridge.");
                    } else {
                        completeStatus("permission_denied", "Shizuku access was not granted to OpenADB Bridge.");
                    }
                }
            };

    private final Runnable binderTimeout = new Runnable() {
        @Override
        public void run() {
            if (!operationStarted && !terminal.get()) {
                completeStatus(
                        "unavailable",
                        "Shizuku is not running or did not provide its binder before timeout."
                );
            }
        }
    };

    private final Runnable permissionTimeout = new Runnable() {
        @Override
        public void run() {
            completeStatus("permission_timeout", "The Shizuku permission request timed out.");
        }
    };

    private final Runnable userServiceBindTimeout = new Runnable() {
        @Override
        public void run() {
            completeStatus("service_failed", "The Shizuku UserService did not connect before timeout.");
        }
    };

    private final ServiceConnection userServiceConnection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder binder) {
            mainHandler.removeCallbacks(userServiceBindTimeout);
            if (terminal.get()) {
                return;
            }
            if (binder == null || !binder.pingBinder()) {
                completeStatus("service_failed", "Shizuku returned an invalid UserService binder.");
                return;
            }
            commandService = IShizukuCommandService.Stub.asInterface(binder);
            final int runtimeUid;
            try {
                runtimeUid = commandService.getRuntimeUid();
            } catch (RemoteException failure) {
                completeStatus("service_failed", "OpenADB could not query the Shizuku UserService identity.");
                return;
            }
            int reportedUid = safeShizukuUid();
            if ((runtimeUid != 0 && runtimeUid != 2000)
                    || (reportedUid >= 0 && runtimeUid != reportedUid)) {
                completeStatus("identity_mismatch", "Shizuku returned an unexpected UserService identity.");
                return;
            }

            showStatusText("The Shizuku UserService accepted the request.");
            Thread execution = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        boolean published = commandService.execute(requestId, timeoutSeconds);
                        if (published) {
                            completeExecution();
                        } else {
                            completeStatus(
                                    "service_failed",
                                    "The Shizuku UserService could not publish a safe result."
                            );
                        }
                    } catch (Throwable failure) {
                        completeStatus(
                                "service_failed",
                                "The Shizuku UserService could not complete the request ("
                                        + failure.getClass().getSimpleName() + ")."
                        );
                    }
                }
            }, "OpenADB-Shizuku-Execute");
            execution.start();
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            commandService = null;
            if (!terminal.get()) {
                completeStatus("service_disconnected", "The Shizuku UserService disconnected unexpectedly.");
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        operation = normalizedOperation(getIntent().getStringExtra("operation"));
        passiveOperation = !"requestpermission".equals(operation);
        if (passiveOperation) {
            setTheme(android.R.style.Theme_Translucent_NoTitleBar);
        }
        super.onCreate(savedInstanceState);
        if (passiveOperation) {
            configurePassiveWindow();
            if (isTaskRoot()) {
                setCurrentTaskExcludedFromRecents(true);
            }
        }
        statusView = new TextView(this);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(32, 32, 32, 32);
        statusView.setTextSize(18.0f);
        statusView.setText("OpenADB Bridge is checking Shizuku…");
        if (passiveOperation) {
            statusView.setVisibility(View.INVISIBLE);
        }
        setContentView(statusView);

        Intent intent = getIntent();
        requestId = intent.getStringExtra("request_id");
        String requestedPermissionHostId = intent.getStringExtra(
                "permission_host_request_id"
        );
        if ("requestpermission".equals(operation)
                && ShizukuProtocol.isValidRequestId(requestedPermissionHostId)) {
            permissionHostRequestId = requestedPermissionHostId;
        }
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            statusView.setText("OpenADB Bridge rejected an invalid Shizuku request identifier.");
            releasePermissionHost("invalid_request");
            finishSoon();
            return;
        }
        timeoutSeconds = ShizukuProtocol.boundedTimeout(intent.getIntExtra("timeout_seconds", 60));
        if ("cancel".equals(operation)) {
            terminal.set(true);
            File marker = writeCancellationMarker();
            signalActiveRequest(requestId);
            if (marker != null) {
                scheduleCancellationMarkerCleanup(marker);
            }
            statusView.setText("OpenADB Bridge cancelled the Shizuku request.");
            finishSoon();
            return;
        }
        if (!"status".equals(operation)
                && !"requestpermission".equals(operation)
                && !"execute".equals(operation)) {
            completeStatus("invalid_request", "OpenADB Bridge rejected an unknown Shizuku operation.");
            return;
        }

        registerActiveRequest();
        mainHandler.postDelayed(cancellationPoll, CANCELLATION_POLL_MILLIS);
        registerListeners();
        mainHandler.postDelayed(binderTimeout, BINDER_WAIT_MILLIS);
        if (safePingBinder()) {
            handleAvailableBinder();
        } else {
            showStatusText("OpenADB Bridge is waiting for Shizuku.");
        }
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        foregroundResumed = true;
        maybeStartForegroundPermissionRequest();
    }

    @Override
    protected void onPause() {
        foregroundResumed = false;
        windowHasFocus = false;
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        windowHasFocus = hasFocus;
        if (hasFocus) {
            maybeStartForegroundPermissionRequest();
        }
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacks(binderTimeout);
        mainHandler.removeCallbacks(permissionTimeout);
        mainHandler.removeCallbacks(userServiceBindTimeout);
        mainHandler.removeCallbacks(cancellationPoll);
        unregisterActiveRequest();
        removeListeners();
        boolean interrupted = terminal.compareAndSet(false, true);
        if (interrupted && commandService != null && ShizukuProtocol.isValidRequestId(requestId)) {
            try {
                commandService.cancel(requestId);
            } catch (RemoteException ignored) {
            }
        }
        if (interrupted && ShizukuProtocol.isValidRequestId(requestId)) {
            writeStatus(
                    "activity_destroyed",
                    "Android closed the OpenADB Shizuku activity before completion."
            );
            releasePermissionHost("activity_destroyed");
        }
        cleanupUserService();
        super.onDestroy();
    }

    private void registerListeners() {
        if (listenersRegistered) {
            return;
        }
        Shizuku.addBinderReceivedListenerSticky(binderReceivedListener, mainHandler);
        Shizuku.addBinderDeadListener(binderDeadListener, mainHandler);
        Shizuku.addRequestPermissionResultListener(permissionResultListener, mainHandler);
        listenersRegistered = true;
    }

    private void removeListeners() {
        if (!listenersRegistered) {
            return;
        }
        Shizuku.removeBinderReceivedListener(binderReceivedListener);
        Shizuku.removeBinderDeadListener(binderDeadListener);
        Shizuku.removeRequestPermissionResultListener(permissionResultListener);
        listenersRegistered = false;
    }

    private void handleAvailableBinder() {
        if (operationStarted || terminal.get() || !safePingBinder()) {
            return;
        }
        if ("requestpermission".equals(operation)
                && (!foregroundResumed || !windowHasFocus)) {
            showStatusText("OpenADB Bridge is bringing the Shizuku request to the foreground.");
            return;
        }
        operationStarted = true;
        mainHandler.removeCallbacks(binderTimeout);
        int api = safeShizukuApi();
        if (api < 11) {
            completeStatus("unsupported", "Shizuku API version 11 or newer is required.");
            return;
        }
        if ("status".equals(operation)) {
            if (safePermission() == PackageManager.PERMISSION_GRANTED) {
                completeStatus("ready", "Shizuku is running and OpenADB Bridge has permission.");
            } else {
                completeStatus("permission_required", "Shizuku is running but permission is required.");
            }
            return;
        }
        if ("requestpermission".equals(operation)) {
            requestPermission();
            return;
        }
        if (safePermission() != PackageManager.PERMISSION_GRANTED) {
            completeStatus("permission_required", "Grant Shizuku access before executing a command.");
            return;
        }
        bindCommandService();
    }

    private void maybeStartForegroundPermissionRequest() {
        if (passiveOperation
                || terminal.get()
                || !"requestpermission".equals(operation)
                || !foregroundResumed
                || !windowHasFocus) {
            return;
        }
        if (safePingBinder()) {
            handleAvailableBinder();
        } else {
            showStatusText("OpenADB Bridge is waiting for Shizuku.");
        }
    }

    private void requestPermission() {
        if (safePermission() == PackageManager.PERMISSION_GRANTED) {
            completeStatus("permission_granted", "Shizuku access is already granted.");
            return;
        }
        try {
            if (Shizuku.shouldShowRequestPermissionRationale()) {
                completeStatus(
                        "permission_denied",
                        "Shizuku permission was denied permanently; change it in the Shizuku app."
                );
                return;
            }
            showStatusText("Confirm the Shizuku permission request on this device.");
            long permissionWait = Math.max(5000L, (timeoutSeconds - 5L) * 1000L);
            mainHandler.postDelayed(permissionTimeout, permissionWait);
            Shizuku.requestPermission(REQUEST_PERMISSION_CODE);
        } catch (Throwable failure) {
            completeStatus("permission_failed", "Android could not open the Shizuku permission request.");
        }
    }

    private void bindCommandService() {
        userServiceArgs = new Shizuku.UserServiceArgs(
                new ComponentName(getPackageName(), ShizukuCommandService.class.getName())
        )
                .daemon(false)
                .tag("openadb-command-v1")
                .processNameSuffix("openadb_shizuku")
                .debuggable(false)
                .version((int) BuildInfo.VERSION_CODE);
        showStatusText("OpenADB Bridge is starting its Shizuku UserService.");
        try {
            userServiceBound = true;
            mainHandler.postDelayed(userServiceBindTimeout, USER_SERVICE_BIND_WAIT_MILLIS);
            Shizuku.bindUserService(userServiceArgs, userServiceConnection);
        } catch (Throwable failure) {
            userServiceBound = false;
            completeStatus("service_failed", "Android could not start the Shizuku UserService.");
        }
    }

    private void completeStatus(final String state, final String message) {
        if (!terminal.compareAndSet(false, true)) {
            return;
        }
        mainHandler.removeCallbacks(binderTimeout);
        mainHandler.removeCallbacks(permissionTimeout);
        mainHandler.removeCallbacks(userServiceBindTimeout);
        mainHandler.removeCallbacks(cancellationPoll);
        unregisterActiveRequest();
        if (cancellationMarker().isFile()) {
            deleteCancellationMarker();
            cleanupUserService();
            removeListeners();
            releasePermissionHost("cancelled");
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (statusView != null) {
                        statusView.setText("The OpenADB Shizuku request was cancelled.");
                    }
                    finishSoon();
                }
            });
            return;
        }
        deleteCancellationMarker();
        writeStatus(state, message);
        releasePermissionHost(state);
        cleanupUserService();
        removeListeners();
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (statusView != null) {
                    statusView.setText(message);
                }
                finishSoon();
            }
        });
    }

    private void completeExecution() {
        if (!terminal.compareAndSet(false, true)) {
            return;
        }
        mainHandler.removeCallbacks(binderTimeout);
        mainHandler.removeCallbacks(permissionTimeout);
        mainHandler.removeCallbacks(userServiceBindTimeout);
        mainHandler.removeCallbacks(cancellationPoll);
        unregisterActiveRequest();
        deleteCancellationMarker();
        cleanupUserService();
        removeListeners();
        releasePermissionHost("cancelled");
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (statusView != null) {
                    statusView.setText("The Shizuku UserService finished the request.");
                }
                finishSoon();
            }
        });
    }

    private synchronized void cleanupUserService() {
        if (!userServiceBound || userServiceArgs == null) {
            return;
        }
        try {
            Shizuku.unbindUserService(userServiceArgs, userServiceConnection, true);
        } catch (Throwable ignored) {
        } finally {
            userServiceBound = false;
            commandService = null;
        }
    }

    private void completeHostCancellation() {
        if (!terminal.compareAndSet(false, true)) {
            return;
        }
        mainHandler.removeCallbacks(binderTimeout);
        mainHandler.removeCallbacks(permissionTimeout);
        mainHandler.removeCallbacks(userServiceBindTimeout);
        mainHandler.removeCallbacks(cancellationPoll);
        unregisterActiveRequest();
        IShizukuCommandService service = commandService;
        if (service != null && ShizukuProtocol.isValidRequestId(requestId)) {
            try {
                service.cancel(requestId);
            } catch (RemoteException ignored) {
            }
        }
        deleteCancellationMarker();
        cleanupUserService();
        removeListeners();
        releasePermissionHost("cancelled");
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (statusView != null) {
                    statusView.setText("The OpenADB Shizuku request was cancelled.");
                }
                finishSoon();
            }
        });
    }

    private void registerActiveRequest() {
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            return;
        }
        synchronized (ACTIVE_REQUESTS_LOCK) {
            ACTIVE_REQUESTS.put(requestId, new WeakReference<ShizukuActivity>(this));
            activeRequestRegistered = true;
        }
    }

    private void releasePermissionHost(String reason) {
        String hostRequestId = permissionHostRequestId;
        permissionHostRequestId = "";
        if (!ShizukuProtocol.isValidRequestId(hostRequestId)) {
            return;
        }
        PermissionHostActivity.completeRequest(
                getApplicationContext(),
                hostRequestId,
                reason
        );
    }

    private void unregisterActiveRequest() {
        if (!activeRequestRegistered) {
            return;
        }
        synchronized (ACTIVE_REQUESTS_LOCK) {
            WeakReference<ShizukuActivity> reference = ACTIVE_REQUESTS.get(requestId);
            if (reference == null || reference.get() == this) {
                ACTIVE_REQUESTS.remove(requestId);
            }
            activeRequestRegistered = false;
        }
    }

    private static void signalActiveRequest(String requestId) {
        final ShizukuActivity active;
        synchronized (ACTIVE_REQUESTS_LOCK) {
            WeakReference<ShizukuActivity> reference = ACTIVE_REQUESTS.get(requestId);
            active = reference == null ? null : reference.get();
            if (active == null) {
                ACTIVE_REQUESTS.remove(requestId);
                return;
            }
        }
        if (active.terminal.get() || active.isFinishing() || active.isDestroyed()) {
            return;
        }
        active.mainHandler.post(new Runnable() {
            @Override
            public void run() {
                active.completeHostCancellation();
            }
        });
    }

    private File cancellationMarker() {
        File directory = new File(getNoBackupFilesDir(), "openadb");
        return new File(directory, "shizuku_cancel_" + requestId + ".marker");
    }

    private File cancellationMarkerTemporary() {
        File directory = new File(getNoBackupFilesDir(), "openadb");
        return new File(directory, ".shizuku_cancel_" + requestId + ".tmp");
    }

    private File writeCancellationMarker() {
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            return null;
        }
        File destination = cancellationMarker();
        File temporary = cancellationMarkerTemporary();
        File directory = destination.getParentFile();
        if (directory == null || (!directory.exists() && !directory.mkdirs())) {
            return null;
        }
        byte[] body = ("OPENADB_SHIZUKU_CANCEL 1\nrequest_id=" + requestId + "\n")
                .getBytes(StandardCharsets.UTF_8);
        temporary.delete();
        try {
            FileOutputStream output = new FileOutputStream(temporary, false);
            try {
                output.write(body);
                output.flush();
                output.getFD().sync();
            } finally {
                output.close();
            }
            Os.rename(temporary.getAbsolutePath(), destination.getAbsolutePath());
            return destination;
        } catch (Throwable ignored) {
            temporary.delete();
            return null;
        }
    }

    private void deleteCancellationMarker() {
        cancellationMarkerTemporary().delete();
        cancellationMarker().delete();
    }

    private static void scheduleCancellationMarkerCleanup(final File marker) {
        PROCESS_HANDLER.postDelayed(new Runnable() {
            @Override
            public void run() {
                marker.delete();
            }
        }, CANCELLATION_MARKER_RETENTION_MILLIS);
    }

    private void writeStatus(String state, String message) {
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            return;
        }
        boolean binder = safePingBinder();
        int uid = binder ? safeShizukuUid() : -1;
        int api = binder ? safeShizukuApi() : -1;
        String permission = permissionName(binder, api);
        boolean installed = binder || packageInstalled("moe.shizuku.privileged.api") || packageInstalled("rikka.sui");
        StringBuilder body = new StringBuilder(512);
        body.append(ShizukuProtocol.STATUS_HEADER).append('\n');
        body.append("request_id=").append(requestId).append('\n');
        body.append("state=").append(safeValue(state)).append('\n');
        body.append("installed=").append(installed ? 1 : 0).append('\n');
        body.append("binder=").append(binder ? 1 : 0).append('\n');
        body.append("permission=").append(permission).append('\n');
        body.append("uid=").append(uid).append('\n');
        body.append("mode=").append(ShizukuProtocol.modeForUid(uid)).append('\n');
        body.append("api=").append(api).append('\n');
        body.append("message_b64=").append(ShizukuProtocol.base64Message(message)).append('\n');
        HostStatusStore.publish(
                this,
                HostStatusStore.KIND_SHIZUKU,
                requestId,
                body.toString()
        );
        writeStatusAtomically(body.toString());
        showStatusText(message);
    }

    private void showStatusText(final String message) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (statusView != null) {
                    statusView.setText(message);
                }
            }
        });
    }

    private void writeStatusAtomically(String contents) {
        File external = getExternalFilesDir(null);
        if (external == null) {
            return;
        }
        File outputDir = new File(external, "openadb");
        if (!outputDir.exists() && !outputDir.mkdirs()) {
            return;
        }
        File destination = new File(outputDir, "shizuku_status_" + requestId + ".txt");
        File temporary = new File(outputDir, ".shizuku_status_" + requestId + ".tmp");
        byte[] bytes = contents.getBytes(StandardCharsets.UTF_8);
        try {
            FileOutputStream output = new FileOutputStream(temporary, false);
            try {
                output.write(bytes);
                output.flush();
                output.getFD().sync();
            } finally {
                output.close();
            }
            try {
                Os.rename(temporary.getAbsolutePath(), destination.getAbsolutePath());
            } catch (Throwable renameFailure) {
                if (destination.exists()) {
                    destination.delete();
                }
                temporary.renameTo(destination);
            }
        } catch (Throwable ignored) {
            temporary.delete();
        }
    }

    private String permissionName(boolean binder, int api) {
        if (!binder) {
            return "unknown";
        }
        if (api < 11) {
            return "unsupported";
        }
        if (safePermission() == PackageManager.PERMISSION_GRANTED) {
            return "granted";
        }
        try {
            return Shizuku.shouldShowRequestPermissionRationale() ? "denied" : "required";
        } catch (Throwable ignored) {
            return "required";
        }
    }

    private boolean packageInstalled(String packageName) {
        try {
            getPackageManager().getPackageInfo(packageName, 0);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static boolean safePingBinder() {
        try {
            return Shizuku.pingBinder();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static int safeShizukuUid() {
        try {
            return Shizuku.getUid();
        } catch (Throwable ignored) {
            return -1;
        }
    }

    private static int safeShizukuApi() {
        try {
            return Shizuku.getVersion();
        } catch (Throwable ignored) {
            return -1;
        }
    }

    private static int safePermission() {
        try {
            return Shizuku.checkSelfPermission();
        } catch (Throwable ignored) {
            return PackageManager.PERMISSION_DENIED;
        }
    }

    private static String normalizedOperation(String value) {
        return value == null ? "" : value.trim().toLowerCase(Locale.US);
    }

    private static String safeValue(String value) {
        if (value == null || !value.matches("[a-z0-9_]+")) {
            return "unknown";
        }
        return value;
    }

    private void configurePassiveWindow() {
        Window window = getWindow();
        window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
        window.clearFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
        window.addFlags(
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
        );
    }

    private void setCurrentTaskExcludedFromRecents(boolean excluded) {
        try {
            ActivityManager manager = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
            if (manager == null) {
                return;
            }
            for (ActivityManager.AppTask task : manager.getAppTasks()) {
                ActivityManager.RecentTaskInfo info = task.getTaskInfo();
                if (info != null && info.id == getTaskId()) {
                    task.setExcludeFromRecents(excluded);
                    return;
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private void finishSoon() {
        mainHandler.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (!passiveOperation && isTaskRoot()) {
                    finishAndRemoveTask();
                } else {
                    finish();
                }
                if (passiveOperation) {
                    overridePendingTransition(0, 0);
                }
            }
        }, passiveOperation ? 0L : 100L);
    }
}
