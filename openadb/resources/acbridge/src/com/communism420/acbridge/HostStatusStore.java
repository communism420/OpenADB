package com.communism420.acbridge;

import android.content.Context;
import android.net.Uri;
import android.system.Os;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * App-private, DUMP-readable status storage for the desktop control plane.
 *
 * <p>Android 16 scoped-storage ownership can make a shell-created file below
 * {@code /sdcard} impossible for a freshly installed application UID to
 * replace, even when the inode is mode 0666.  Status payloads therefore live
 * in ACBridge internal storage and are exposed read-only through
 * {@link HostStatusProvider}.  Request ids remain random and every desktop
 * parser still authenticates the protocol body.</p>
 */
final class HostStatusStore {
    static final String AUTHORITY = "com.communism420.acbridge.openadb.status";
    static final String KIND_SHIZUKU = "shizuku";
    static final String KIND_PRIVILEGE = "privilege";
    static final String KIND_PERMISSION_HOST = "permission_host";
    private static final String TAG = "OpenADBStatusStore";
    private static final String DIRECTORY = "openadb-host-status";

    private HostStatusStore() {
    }

    static Uri uri(String kind, String requestId) {
        return new Uri.Builder()
                .scheme("content")
                .authority(AUTHORITY)
                .appendPath(kind)
                .appendPath(requestId)
                .build();
    }

    static boolean publish(
            Context context,
            String kind,
            String requestId,
            String contents
    ) {
        File destination = resolve(context, kind, requestId);
        if (destination == null) {
            return false;
        }
        File directory = destination.getParentFile();
        if (directory == null || (!directory.exists() && !directory.mkdirs())) {
            Log.e(TAG, "Could not create the app-private status directory");
            return false;
        }
        File temporary = new File(directory, "." + destination.getName() + ".tmp");
        byte[] bytes = String.valueOf(contents == null ? "" : contents)
                .getBytes(StandardCharsets.UTF_8);
        try {
            if (temporary.exists() && !temporary.delete()) {
                Log.e(TAG, "Could not replace a stale app-private status temporary file");
                return false;
            }
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
                if (destination.exists() && !destination.delete()) {
                    throw renameFailure;
                }
                if (!temporary.renameTo(destination)) {
                    throw renameFailure;
                }
            }
            return destination.isFile() && destination.length() > 0L;
        } catch (Throwable failure) {
            temporary.delete();
            Log.e(TAG, "Could not publish an app-private OpenADB status", failure);
            return false;
        }
    }

    static File resolve(Context context, Uri uri) {
        if (uri == null || !"content".equals(uri.getScheme())
                || !AUTHORITY.equals(uri.getAuthority())) {
            return null;
        }
        List<String> segments = uri.getPathSegments();
        if (segments.size() != 2) {
            return null;
        }
        return resolve(context, segments.get(0), segments.get(1));
    }

    static File resolve(Context context, String kind, String requestId) {
        if (context == null || !validKind(kind) || !validRequestId(requestId)) {
            return null;
        }
        File directory = new File(context.getNoBackupFilesDir(), DIRECTORY);
        return new File(directory, kind + "_status_" + requestId + ".txt");
    }

    static boolean delete(Context context, String kind, String requestId) {
        File destination = resolve(context, kind, requestId);
        if (destination == null) {
            return false;
        }
        File temporary = new File(
                destination.getParentFile(),
                "." + destination.getName() + ".tmp"
        );
        boolean existed = destination.exists() || temporary.exists();
        boolean destinationDeleted = !destination.exists() || destination.delete();
        boolean temporaryDeleted = !temporary.exists() || temporary.delete();
        return existed && destinationDeleted && temporaryDeleted;
    }

    private static boolean validKind(String kind) {
        return KIND_SHIZUKU.equals(kind)
                || KIND_PRIVILEGE.equals(kind)
                || KIND_PERMISSION_HOST.equals(kind);
    }

    private static boolean validRequestId(String requestId) {
        return ShizukuProtocol.isValidRequestId(requestId);
    }
}
