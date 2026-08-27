package com.communism420.acbridge;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.widget.TextView;

import java.lang.ref.WeakReference;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Request-scoped foreground task kept alive while Android presents a permission UI.
 *
 * <p>The interactive Root/Shizuku activity closes this host only after it has
 * durably published a terminal permission result. A DUMP-protected receiver is
 * retained as a desktop-side fallback for cancellation, transport errors, and
 * application shutdown. Both paths compare the random request token, so a late
 * cleanup from an older access-mode switch cannot close the current host.</p>
 */
public final class PermissionHostActivity extends Activity {
    static final String STATUS_HEADER = "OPENADB_PERMISSION_HOST_STATUS 1";
    private static final String OPEN_OPERATION = "open";
    private static final String PREFERENCES = "openadb-permission-host";
    private static final String ACTIVE_REQUEST_KEY = "active_request_id";
    private static final String ACTIVE_BACKEND_KEY = "active_backend";
    private static final String CLOSING_REQUEST_KEY = "closing_request_id";
    private static final String CLOSE_PENDING_EXTRA = "close_pending";
    private static final int MIN_TIMEOUT_SECONDS = 30;
    private static final int MAX_TIMEOUT_SECONDS = 900;
    private static final int MAX_READY_PUBLISH_ATTEMPTS = 20;
    private static final Object ACTIVE_REQUEST_LOCK = new Object();
    private static WeakReference<PermissionHostActivity> activeRequest =
            new WeakReference<PermissionHostActivity>(null);

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean finished = new AtomicBoolean(false);
    private String requestId = "";
    private String backend = "";
    private boolean hostOpened;
    private boolean foregroundResumed;
    private boolean windowHasFocus;
    private boolean readyPublished;
    private int readyPublishAttempts;
    private String terminalCloseRequestId = "";
    private TextView statusView;

    private final Runnable safetyTimeout = new Runnable() {
        @Override
        public void run() {
            finishHost(requestId, "orphan_timeout");
        }
    };

    private final Runnable readyPublishRetry = new Runnable() {
        @Override
        public void run() {
            maybePublishReady();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        statusView = new TextView(this);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(32, 32, 32, 32);
        statusView.setTextSize(18.0f);
        setContentView(statusView);

        Intent intent = getIntent();
        String operation = PrivilegeProtocol.normalizedOperation(
                intent == null ? null : intent.getStringExtra("operation")
        );
        String requestedId = intent == null ? null : intent.getStringExtra("request_id");
        String requestedBackend = normalizedHostBackend(
                intent == null ? null : intent.getStringExtra("backend")
        );
        if (!OPEN_OPERATION.equals(operation)
                || !PrivilegeProtocol.isValidRequestId(requestedId)
                || (!"root".equals(requestedBackend) && !"shizuku".equals(requestedBackend))) {
            finish();
            return;
        }
        if (savedInstanceState != null
                && !requestedId.equals(persistedActiveRequest(this))) {
            publishStatus(this, requestedId, "closed");
            finishAndRemoveTask();
            return;
        }
        boolean closePending = requestedId.equals(persistedClosingRequest(this))
                || intent.getBooleanExtra(CLOSE_PENDING_EXTRA, false);
        activateRequest(
                requestedId,
                requestedBackend,
                intent.getIntExtra("timeout_seconds", 300)
        );
        if (closePending) {
            finishHost(requestedId, "pending_completion");
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String operation = PrivilegeProtocol.normalizedOperation(
                intent == null ? null : intent.getStringExtra("operation")
        );
        String requestedId = intent == null ? null : intent.getStringExtra("request_id");
        String requestedBackend = normalizedHostBackend(
                intent == null ? null : intent.getStringExtra("backend")
        );
        if (!OPEN_OPERATION.equals(operation)
                || !PrivilegeProtocol.isValidRequestId(requestedId)
                || (!"root".equals(requestedBackend) && !"shizuku".equals(requestedBackend))) {
            return;
        }
        boolean closePending = requestedId.equals(persistedClosingRequest(this))
                || intent.getBooleanExtra(CLOSE_PENDING_EXTRA, false);
        if (hostOpened) {
            if (!requestId.equals(requestedId)) {
                // A singleTask must never be reassigned while its current
                // permission workflow is live or closing. The caller for this
                // unaccepted token receives no ready acknowledgement and can
                // retry after the current task has actually been destroyed.
                publishStatus(this, requestedId, "closed");
                return;
            }
            setIntent(intent);
            if (closePending) {
                finishHost(requestedId, "pending_completion");
            } else {
                maybePublishReady();
            }
            return;
        }
        setIntent(intent);
        activateRequest(
                requestedId,
                requestedBackend,
                intent.getIntExtra("timeout_seconds", 300)
        );
        if (closePending) {
            finishHost(requestedId, "pending_completion");
        }
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        foregroundResumed = true;
        maybePublishReady();
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
            maybePublishReady();
        }
    }

    @Override
    public void onBackPressed() {
        if (hostOpened && !finished.get()) {
            statusView.setText(
                    "Complete or cancel the permission request before closing OpenADB Bridge."
            );
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacks(safetyTimeout);
        mainHandler.removeCallbacks(readyPublishRetry);
        String closedRequestId = terminalCloseRequestId;
        boolean terminalClose = finished.get()
                && PrivilegeProtocol.isValidRequestId(closedRequestId)
                && closedRequestId.equals(requestId);
        Context applicationContext = getApplicationContext();
        super.onDestroy();
        if (terminalClose) {
            clearPersistedRequestIfMatching(applicationContext, closedRequestId);
        }
        unregisterActiveRequest();
        if (terminalClose) {
            publishStatus(applicationContext, closedRequestId, "closed");
        }
    }

    static void completeRequest(Context context, String completedRequestId, String reason) {
        if (context == null || !PrivilegeProtocol.isValidRequestId(completedRequestId)) {
            return;
        }
        final Context applicationContext = context.getApplicationContext();
        if (Looper.myLooper() != Looper.getMainLooper()) {
            new Handler(Looper.getMainLooper()).post(new Runnable() {
                @Override
                public void run() {
                    completeRequest(applicationContext, completedRequestId, reason);
                }
            });
            return;
        }
        final PermissionHostActivity active;
        synchronized (ACTIVE_REQUEST_LOCK) {
            active = activeRequest.get();
        }
        if (active != null) {
            // finishHost repeats the token comparison on the main thread. A
            // completion queued for request A therefore cannot close request B
            // if singleTask delivered B through onNewIntent in the meantime.
            active.finishHost(completedRequestId, reason);
            return;
        }

        // An unexpected Activity recreation can briefly leave the persisted
        // token without a live Java instance. Mark it as closing and relaunch
        // the exact token so onDestroy remains the sole source of closed ACKs.
        if (completedRequestId.equals(persistedActiveRequest(applicationContext))) {
            String closingBackend = markPersistedRequestClosingIfMatching(
                    applicationContext,
                    completedRequestId
            );
            if (!closingBackend.isEmpty()) {
                Intent closeIntent = new Intent(
                        applicationContext,
                        PermissionHostActivity.class
                );
                closeIntent.putExtra("operation", OPEN_OPERATION);
                closeIntent.putExtra("backend", closingBackend);
                closeIntent.putExtra("request_id", completedRequestId);
                closeIntent.putExtra(CLOSE_PENDING_EXTRA, true);
                closeIntent.addFlags(
                        Intent.FLAG_ACTIVITY_NEW_TASK
                                | Intent.FLAG_ACTIVITY_CLEAR_TOP
                                | Intent.FLAG_ACTIVITY_SINGLE_TOP
                );
                try {
                    applicationContext.startActivity(closeIntent);
                } catch (RuntimeException ignored) {
                    // A later protected receiver retry can resume cleanup.
                }
            }
            return;
        }
        // No current or persisted host owns this token. It is already closed;
        // acknowledging a stale cleanup cannot affect a newer request.
        publishStatus(applicationContext, completedRequestId, "closed");
    }

    private void activateRequest(
            String requestedId,
            String requestedBackend,
            int requestedTimeoutSeconds
    ) {
        mainHandler.removeCallbacks(safetyTimeout);
        mainHandler.removeCallbacks(readyPublishRetry);
        requestId = requestedId;
        backend = requestedBackend;
        hostOpened = true;
        readyPublished = false;
        readyPublishAttempts = 0;
        terminalCloseRequestId = "";
        finished.set(false);
        persistActiveRequest(this, requestId, backend);
        synchronized (ACTIVE_REQUEST_LOCK) {
            activeRequest = new WeakReference<PermissionHostActivity>(this);
        }
        statusView.setText(
                "root".equals(backend)
                        ? "Confirm the Root permission requests for Android shell and OpenADB Bridge."
                        : "Confirm the Shizuku permission request for OpenADB Bridge."
        );
        int timeoutSeconds = Math.max(
                MIN_TIMEOUT_SECONDS,
                Math.min(MAX_TIMEOUT_SECONDS, requestedTimeoutSeconds)
        );
        mainHandler.postDelayed(safetyTimeout, timeoutSeconds * 1000L);
        maybePublishReady();
    }

    private void maybePublishReady() {
        if (!hostOpened
                || finished.get()
                || readyPublished) {
            return;
        }
        readyPublishAttempts++;
        // A singleTask receives a new token through onNewIntent(). When it was
        // already the focused top Activity, Android does not always emit a new
        // focus callback. Query the Window directly and retry briefly instead
        // of leaving the replacement token without a ready acknowledgement.
        windowHasFocus = hasWindowFocus();
        if (windowHasFocus) {
            foregroundResumed = true;
        }
        if (!foregroundResumed || !windowHasFocus) {
            if (readyPublishAttempts < MAX_READY_PUBLISH_ATTEMPTS) {
                mainHandler.removeCallbacks(readyPublishRetry);
                mainHandler.postDelayed(readyPublishRetry, 100L);
            }
            return;
        }
        readyPublished = publishStatus(this, requestId, "ready");
        if (!readyPublished && readyPublishAttempts < MAX_READY_PUBLISH_ATTEMPTS) {
            mainHandler.removeCallbacks(readyPublishRetry);
            mainHandler.postDelayed(readyPublishRetry, 100L);
        }
    }

    private void finishHost(final String expectedRequestId, final String reason) {
        if (!PrivilegeProtocol.isValidRequestId(expectedRequestId)) {
            return;
        }
        if (Looper.myLooper() != Looper.getMainLooper()) {
            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    finishHost(expectedRequestId, reason);
                }
            });
            return;
        }
        if (!expectedRequestId.equals(requestId)) {
            // This completion belongs to an older request. Acknowledge that
            // token without touching the currently visible request.
            clearPersistedRequestIfMatching(this, expectedRequestId);
            publishStatus(this, expectedRequestId, "closed");
            return;
        }
        synchronized (ACTIVE_REQUEST_LOCK) {
            PermissionHostActivity active = activeRequest.get();
            String persisted = preferences(this).getString(ACTIVE_REQUEST_KEY, "");
            if (active != this || !expectedRequestId.equals(persisted)) {
                return;
            }
            if (!finished.compareAndSet(false, true)) {
                return;
            }
            boolean markedClosing = preferences(this)
                    .edit()
                    .putString(CLOSING_REQUEST_KEY, expectedRequestId)
                    .commit();
            if (!markedClosing) {
                finished.set(false);
                return;
            }
            terminalCloseRequestId = expectedRequestId;
        }
        mainHandler.removeCallbacks(safetyTimeout);
        mainHandler.removeCallbacks(readyPublishRetry);
        if (isTaskRoot()) {
            finishAndRemoveTask();
        } else {
            finish();
        }
    }

    private void unregisterActiveRequest() {
        synchronized (ACTIVE_REQUEST_LOCK) {
            PermissionHostActivity active = activeRequest.get();
            if (active == this) {
                activeRequest = new WeakReference<PermissionHostActivity>(null);
            }
        }
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    private static String persistedActiveRequest(Context context) {
        synchronized (ACTIVE_REQUEST_LOCK) {
            return preferences(context).getString(ACTIVE_REQUEST_KEY, "");
        }
    }

    private static String persistedClosingRequest(Context context) {
        synchronized (ACTIVE_REQUEST_LOCK) {
            return preferences(context).getString(CLOSING_REQUEST_KEY, "");
        }
    }

    private static void persistActiveRequest(Context context, String id, String activeBackend) {
        synchronized (ACTIVE_REQUEST_LOCK) {
            preferences(context)
                    .edit()
                    .putString(ACTIVE_REQUEST_KEY, id)
                    .putString(ACTIVE_BACKEND_KEY, activeBackend)
                    .remove(CLOSING_REQUEST_KEY)
                    .commit();
        }
    }

    private static String markPersistedRequestClosingIfMatching(
            Context context,
            String id
    ) {
        synchronized (ACTIVE_REQUEST_LOCK) {
            SharedPreferences state = preferences(context);
            if (!id.equals(state.getString(ACTIVE_REQUEST_KEY, ""))) {
                return "";
            }
            String activeBackend = normalizedHostBackend(
                    state.getString(ACTIVE_BACKEND_KEY, "")
            );
            if (activeBackend.isEmpty()) {
                return "";
            }
            boolean committed = state.edit()
                    .putString(CLOSING_REQUEST_KEY, id)
                    .commit();
            return committed ? activeBackend : "";
        }
    }

    private static boolean clearPersistedRequestIfMatching(Context context, String id) {
        synchronized (ACTIVE_REQUEST_LOCK) {
            SharedPreferences state = preferences(context);
            if (!id.equals(state.getString(ACTIVE_REQUEST_KEY, ""))) {
                return false;
            }
            return state.edit()
                    .remove(ACTIVE_REQUEST_KEY)
                    .remove(ACTIVE_BACKEND_KEY)
                    .remove(CLOSING_REQUEST_KEY)
                    .commit();
        }
    }

    private static boolean publishStatus(Context context, String id, String state) {
        if (!PrivilegeProtocol.isValidRequestId(id)) {
            return false;
        }
        String body = STATUS_HEADER + '\n'
                + "request_id=" + id + '\n'
                + "state=" + state + '\n';
        return HostStatusStore.publish(
                context,
                HostStatusStore.KIND_PERMISSION_HOST,
                id,
                body
        );
    }

    private static String normalizedHostBackend(String value) {
        String normalized = value == null ? "" : value.trim().toLowerCase(Locale.US);
        return "root".equals(normalized) || "shizuku".equals(normalized)
                ? normalized
                : "";
    }
}
