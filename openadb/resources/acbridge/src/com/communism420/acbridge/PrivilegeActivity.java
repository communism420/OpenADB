package com.communism420.acbridge;

import android.app.Activity;
import android.app.ActivityManager;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.system.Os;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/** DUMP-protected foreground handshake for ACBridge's own Root permission. */
public final class PrivilegeActivity extends Activity {
    private static final Object ACTIVE_REQUESTS_LOCK = new Object();
    private static final Map<String, WeakReference<PrivilegeActivity>> ACTIVE_REQUESTS =
            new HashMap<String, WeakReference<PrivilegeActivity>>();

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean terminal = new AtomicBoolean(false);
    private String requestId = "";
    private String permissionHostRequestId = "";
    private String backend = "";
    private boolean activeRequestRegistered;
    private boolean passiveOperation;
    private boolean foregroundResumed;
    private boolean windowHasFocus;
    private final AtomicBoolean rootRequestStarted = new AtomicBoolean(false);
    private volatile Process rootProcess;
    private TextView statusView;

    private final Runnable rootTimeout = new Runnable() {
        @Override
        public void run() {
            complete(
                    "timed_out",
                    "unknown",
                    -1,
                    "OpenADB Bridge timed out while waiting for its Root permission."
            );
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        String operation = PrivilegeProtocol.normalizedOperation(
                getIntent().getStringExtra("operation")
        );
        passiveOperation = PrivilegeProtocol.CANCEL_OPERATION.equals(operation);
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
        statusView.setText("OpenADB Bridge is preparing its access request…");
        if (passiveOperation) {
            statusView.setVisibility(View.INVISIBLE);
        }
        setContentView(statusView);

        requestId = getIntent().getStringExtra("request_id");
        String requestedPermissionHostId = getIntent().getStringExtra(
                "permission_host_request_id"
        );
        if (PrivilegeProtocol.isValidRequestId(requestedPermissionHostId)) {
            permissionHostRequestId = requestedPermissionHostId;
        }
        if (!PrivilegeProtocol.isValidRequestId(requestId)) {
            terminal.set(true);
            statusView.setText("OpenADB Bridge rejected an invalid access request identifier.");
            releasePermissionHost("invalid_request");
            finishSoon();
            return;
        }
        if (PrivilegeProtocol.CANCEL_OPERATION.equals(operation)) {
            terminal.set(true);
            signalActiveRequest(requestId);
            statusView.setText("OpenADB Bridge cancelled the access request.");
            finishSoon();
            return;
        }
        if (!PrivilegeProtocol.REQUEST_OPERATION.equals(operation)) {
            terminal.set(true);
            statusView.setText("OpenADB Bridge rejected an unknown access operation.");
            releasePermissionHost("invalid_request");
            finishSoon();
            return;
        }

        backend = PrivilegeProtocol.normalizedBackend(getIntent().getStringExtra("backend"));
        if (backend.length() == 0) {
            complete(
                    "invalid_request",
                    "unknown",
                    -1,
                    "OpenADB Bridge rejected an unknown access backend."
            );
            return;
        }
        registerActiveRequest();
        if ("standard".equals(backend)) {
            complete(
                    "ready",
                    "not_required",
                    android.os.Process.myUid(),
                    "Standard ACBridge access does not require Root or Shizuku permission."
            );
            return;
        }

        int timeoutSeconds = PrivilegeProtocol.boundedTimeout(
                getIntent().getIntExtra("timeout_seconds", 180)
        );
        statusView.setText("Grant Root permission to OpenADB Bridge on this device.");
        mainHandler.postDelayed(rootTimeout, timeoutSeconds * 1000L);
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        foregroundResumed = true;
        maybeStartRootRequest();
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
            maybeStartRootRequest();
        }
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacks(rootTimeout);
        unregisterActiveRequest();
        if (terminal.compareAndSet(false, true)) {
            destroyRootProcess();
            writeStatus(
                    "activity_destroyed",
                    "unknown",
                    -1,
                    "Android closed the OpenADB Bridge access activity before completion."
            );
            releasePermissionHost("activity_destroyed");
        }
        super.onDestroy();
    }

    private void requestRootAccess() {
        Process process = null;
        BufferedReader reader = null;
        StringBuilder output = new StringBuilder(64);
        boolean outputTruncated = false;
        try {
            // This command is deliberately fixed. No command or shell text is accepted
            // from the launching Intent.
            process = new ProcessBuilder("su", "-c", "id -u")
                    .redirectErrorStream(true)
                    .start();
            rootProcess = process;
            reader = new BufferedReader(
                    new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8),
                    4096
            );
            char[] buffer = new char[4096];
            int read;
            while ((read = reader.read(buffer)) >= 0) {
                if (read == 0) {
                    continue;
                }
                int remaining = PrivilegeProtocol.MAX_ROOT_OUTPUT_CHARS - output.length();
                int retained = Math.min(read, Math.max(0, remaining));
                if (retained > 0) {
                    output.append(buffer, 0, retained);
                }
                if (retained < read) {
                    outputTruncated = true;
                }
                // Continue draining excess output so the fixed process cannot deadlock.
            }
            int exitCode = process.waitFor();
            rootProcess = null;
            if (terminal.get()) {
                return;
            }
            if (exitCode == 0 && !outputTruncated && "0".equals(output.toString().trim())) {
                complete(
                        "ready",
                        "granted",
                        0,
                        "Root permission is ready for OpenADB Bridge."
                );
            } else {
                complete(
                        "denied",
                        "denied",
                        -1,
                        "Root permission was denied or did not provide UID 0 to OpenADB Bridge."
                );
            }
        } catch (Throwable failure) {
            rootProcess = null;
            if (!terminal.get()) {
                complete(
                        "unavailable",
                        "unavailable",
                        -1,
                        "OpenADB Bridge could not start a Root permission request."
                );
            }
        } finally {
            if (reader != null) {
                try {
                    reader.close();
                } catch (Throwable ignored) {
                }
            }
            if (process != null) {
                process.destroy();
            }
        }
    }

    private void maybeStartRootRequest() {
        if (passiveOperation
                || terminal.get()
                || !"root".equals(backend)
                || !foregroundResumed
                || !windowHasFocus
                || !rootRequestStarted.compareAndSet(false, true)) {
            return;
        }
        Thread requestThread = new Thread(new Runnable() {
            @Override
            public void run() {
                requestRootAccess();
            }
        }, "OpenADB-Bridge-Root-Permission");
        requestThread.start();
    }

    private void complete(
            final String state,
            final String permission,
            final int uid,
            final String message
    ) {
        if (!terminal.compareAndSet(false, true)) {
            return;
        }
        mainHandler.removeCallbacks(rootTimeout);
        unregisterActiveRequest();
        destroyRootProcess();
        writeStatus(state, permission, uid, message);
        releasePermissionHost(state);
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

    private synchronized void destroyRootProcess() {
        Process process = rootProcess;
        rootProcess = null;
        if (process != null) {
            try {
                process.destroy();
            } catch (Throwable ignored) {
            }
        }
    }

    private void releasePermissionHost(String reason) {
        String hostRequestId = permissionHostRequestId;
        permissionHostRequestId = "";
        if (!PrivilegeProtocol.isValidRequestId(hostRequestId)) {
            return;
        }
        PermissionHostActivity.completeRequest(
                getApplicationContext(),
                hostRequestId,
                reason
        );
    }

    private void writeStatus(String state, String permission, int uid, String message) {
        if (!PrivilegeProtocol.isValidRequestId(requestId)) {
            return;
        }
        String safeBackend = backend.length() == 0 ? "unknown" : backend;
        StringBuilder body = new StringBuilder(384);
        body.append(PrivilegeProtocol.STATUS_HEADER).append('\n');
        body.append("request_id=").append(requestId).append('\n');
        body.append("backend=").append(safeBackend).append('\n');
        body.append("state=").append(state).append('\n');
        body.append("permission=").append(permission).append('\n');
        body.append("uid=").append(uid).append('\n');
        body.append("message_b64=")
                .append(PrivilegeProtocol.base64Message(message))
                .append('\n');

        File publicDirectory = new File(Environment.getExternalStorageDirectory(), ".adac");
        File external = getExternalFilesDir(null);
        File appDirectory = external == null
                ? null
                : new File(external, "openadb");
        HostStatusStore.publish(
                this,
                HostStatusStore.KIND_PRIVILEGE,
                requestId,
                body.toString()
        );
        writeStatusAtomically(publicDirectory, body.toString());
        if (appDirectory != null && !samePath(publicDirectory, appDirectory)) {
            writeStatusAtomically(appDirectory, body.toString());
        }
    }

    private void writeStatusAtomically(File directory, String contents) {
        if (directory == null || (!directory.exists() && !directory.mkdirs())) {
            return;
        }
        String filename = "privilege_status_" + requestId + ".txt";
        File destination = new File(directory, filename);
        File temporary = new File(directory, "." + filename + ".tmp");
        byte[] bytes = contents.getBytes(StandardCharsets.UTF_8);
        try {
            // Older OpenADB versions may pre-create this random,
            // request-scoped file as the ADB shell with mode 0666.  Truncating
            // that inode (instead of
            // deleting and recreating it as the app UID) keeps the atomic
            // result readable through Android 16 scoped-storage mediation.
            // When pre-creation is unavailable, FileOutputStream retains the
            // original app-owned fallback behavior.
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

    private void registerActiveRequest() {
        synchronized (ACTIVE_REQUESTS_LOCK) {
            ACTIVE_REQUESTS.put(requestId, new WeakReference<PrivilegeActivity>(this));
            activeRequestRegistered = true;
        }
    }

    private void unregisterActiveRequest() {
        if (!activeRequestRegistered) {
            return;
        }
        synchronized (ACTIVE_REQUESTS_LOCK) {
            WeakReference<PrivilegeActivity> reference = ACTIVE_REQUESTS.get(requestId);
            if (reference == null || reference.get() == this) {
                ACTIVE_REQUESTS.remove(requestId);
            }
            activeRequestRegistered = false;
        }
    }

    private static void signalActiveRequest(String requestId) {
        final PrivilegeActivity active;
        synchronized (ACTIVE_REQUESTS_LOCK) {
            WeakReference<PrivilegeActivity> reference = ACTIVE_REQUESTS.get(requestId);
            active = reference == null ? null : reference.get();
            if (active == null) {
                ACTIVE_REQUESTS.remove(requestId);
                return;
            }
        }
        active.mainHandler.post(new Runnable() {
            @Override
            public void run() {
                active.complete(
                        "cancelled",
                        "unknown",
                        -1,
                        "The OpenADB Bridge access request was cancelled."
                );
            }
        });
    }

    private static boolean samePath(File left, File right) {
        try {
            return left.getCanonicalFile().equals(right.getCanonicalFile());
        } catch (Throwable ignored) {
            return left.getAbsolutePath().equals(right.getAbsolutePath());
        }
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
