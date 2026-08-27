package com.communism420.acbridge;

import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.regex.Pattern;

/** Constants and validation for the request-scoped ACBridge privilege handshake. */
final class PrivilegeProtocol {
    static final String STATUS_HEADER = "OPENADB_BRIDGE_PRIVILEGE_STATUS 1";
    static final String REQUEST_OPERATION = "requestprivilege";
    static final String CANCEL_OPERATION = "cancelprivilege";
    static final int MIN_TIMEOUT_SECONDS = 5;
    static final int MAX_TIMEOUT_SECONDS = 300;
    static final int MAX_ROOT_OUTPUT_CHARS = 16 * 1024;

    private static final Pattern REQUEST_ID = Pattern.compile("[0-9a-f]{32}");

    private PrivilegeProtocol() {
    }

    static boolean isValidRequestId(String value) {
        return value != null && REQUEST_ID.matcher(value).matches();
    }

    static int boundedTimeout(int seconds) {
        return Math.max(MIN_TIMEOUT_SECONDS, Math.min(MAX_TIMEOUT_SECONDS, seconds));
    }

    static String normalizedOperation(String value) {
        return value == null ? "" : value.trim().toLowerCase(Locale.US);
    }

    static String normalizedBackend(String value) {
        String backend = value == null ? "" : value.trim().toLowerCase(Locale.US);
        return "root".equals(backend) || "standard".equals(backend) ? backend : "";
    }

    static String base64Message(String message) {
        String safe = message == null ? "" : message;
        return Base64.encodeToString(safe.getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
    }
}
