package com.communism420.acbridge;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/** Read-only bridge from ADB shell (DUMP permission) to app-private statuses. */
public final class HostStatusProvider extends ContentProvider {
    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) {
            throw new FileNotFoundException("OpenADB status provider is read-only");
        }
        File file = HostStatusStore.resolve(getContext(), uri);
        if (file == null || !file.isFile() || file.length() <= 0L) {
            throw new FileNotFoundException("OpenADB status is not ready");
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        File file = HostStatusStore.resolve(getContext(), uri);
        if (file == null) {
            return 0;
        }
        return HostStatusStore.delete(
                getContext(),
                uri.getPathSegments().get(0),
                uri.getPathSegments().get(1)
        ) ? 1 : 0;
    }

    @Override
    public String getType(Uri uri) {
        return HostStatusStore.resolve(getContext(), uri) == null
                ? null
                : "text/plain";
    }

    @Override
    public Cursor query(
            Uri uri,
            String[] projection,
            String selection,
            String[] selectionArgs,
            String sortOrder
    ) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("OpenADB status provider is read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("OpenADB status provider is read-only");
    }
}
