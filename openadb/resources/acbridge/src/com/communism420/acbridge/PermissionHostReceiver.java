package com.communism420.acbridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** DUMP-protected, request-scoped fallback used to close the permission host. */
public final class PermissionHostReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null || intent == null) {
            return;
        }
        String operation = PrivilegeProtocol.normalizedOperation(
                intent.getStringExtra("operation")
        );
        if (!"dismiss".equals(operation)) {
            return;
        }
        String requestId = intent.getStringExtra("request_id");
        if (!PrivilegeProtocol.isValidRequestId(requestId)) {
            return;
        }
        PermissionHostActivity.completeRequest(
                context.getApplicationContext(),
                requestId,
                "desktop_cleanup"
        );
    }
}
