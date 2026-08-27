package com.communism420.acbridge;

import android.util.Base64;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.regex.Pattern;

/** Shared constants for the file-based ACBridge/Shizuku protocol. */
final class ShizukuProtocol {
    static final String STATUS_HEADER = "OPENADB_SHIZUKU_STATUS 1";
    static final String REQUEST_HEADER = "OPENADB_SHIZUKU_REQUEST 1";
    static final String RESULT_HEADER = "OPENADB_SHIZUKU_RESULT 1";
    static final String TEMP_PREFIX = "openadb-shizuku-";
    static final File TEMP_ROOT = new File("/data/local/tmp");
    static final int MAX_TIMEOUT_SECONDS = 3600;
    static final int MAX_REQUEST_BYTES = 128 * 1024;
    static final int MAX_ARGUMENT_COUNT = 32;
    static final int MAX_ARGUMENT_BYTES = 64 * 1024;
    static final long MAX_OUTPUT_BYTES = 8L * 1024L * 1024L;

    private static final Pattern REQUEST_ID = Pattern.compile("[0-9a-f]{32}");

    private ShizukuProtocol() {
    }

    static boolean isValidRequestId(String value) {
        return value != null && REQUEST_ID.matcher(value).matches();
    }

    static int boundedTimeout(int seconds) {
        return Math.max(1, Math.min(MAX_TIMEOUT_SECONDS, seconds));
    }

    static File temporaryFile(String requestId, String suffix) {
        if (!isValidRequestId(requestId)) {
            throw new IllegalArgumentException("Invalid request id");
        }
        return new File(TEMP_ROOT, TEMP_PREFIX + requestId + "." + suffix);
    }

    static String modeForUid(int uid) {
        if (uid == 0) {
            return "root";
        }
        if (uid == 2000) {
            return "shell";
        }
        return "unknown";
    }

    static String base64Message(String message) {
        String safe = message == null ? "" : message;
        return Base64.encodeToString(safe.getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
    }
}
