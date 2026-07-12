package com.communism420.acbridge;

import android.app.Activity;
import android.app.UiModeManager;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.UriPermission;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.storage.StorageManager;
import android.os.storage.StorageVolume;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;
import android.provider.MediaStore;
import android.provider.Settings;
import android.widget.TextView;
import android.view.KeyEvent;
import android.view.View;

import java.io.ByteArrayOutputStream;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public final class MainActivity extends Activity {
    private static final String LABEL_SEPARATOR = "\\+\\";
    private static final int REQUEST_STORAGE_TREE = 42042;
    private static final int REQUEST_ALL_FILES_ACCESS = 42043;
    private static final String PREFS = "openadb_bridge";
    private static final String PREF_LAST_TREE_URI = "last_tree_uri";
    private TextView status;
    private String pendingGrantPath = "";
    private boolean pendingGrantEndExit = true;
    private boolean storageGrantPending = false;
    private int storageGrantAttempts = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        status = new TextView(this);
        status.setText("OpenADB Bridge is exporting app data...");
        status.setPadding(24, 24, 24, 24);
        status.setTextSize(isAndroidTv() ? 22.0f : 16.0f);
        status.setFocusable(true);
        setContentView(status);

        Intent intent = getIntent();
        if ("grantStorage".equalsIgnoreCase(intent.getStringExtra("operation"))) {
            requestStorageAccess(intent);
            return;
        }

        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                exportAppData();
            }
        }, "OpenADB-Bridge-Export");
        worker.start();
    }

    private void exportAppData() {
        Intent intent = getIntent();
        boolean endExit = intent.getBooleanExtra("endexit", true);
        File outputDir = outputDir();
        if (!outputDir.exists() && !outputDir.mkdirs()) {
            showStatus("OpenADB Bridge cannot create output folder: " + outputDir.getAbsolutePath(), endExit);
            return;
        }
        Map<String, String> settings = readSettings(new File(outputDir, "settings"));
        String operation = stringExtraOrSetting(intent, settings, "operation", "export");
        if ("delete".equalsIgnoreCase(operation)) {
            deletePath(outputDir, intent, settings, endExit);
            return;
        }
        boolean showIcons = booleanExtraOrSetting(intent, settings, "showicons", true);
        endExit = booleanExtraOrSetting(intent, settings, "endexit", endExit);
        boolean legacyMirror = booleanExtraOrSetting(intent, settings, "legacy", false);
        boolean appSizes = booleanExtraOrSetting(intent, settings, "appsizes", true);
        boolean rootMode = booleanExtraOrSetting(intent, settings, "rootmode", false);
        int iconSize = Math.max(48, Math.min(192, intExtraOrSetting(intent, settings, "iconsize", 96)));

        File dataFile = new File(outputDir, ".acbridge");
        File metadataFile = new File(outputDir, "metadata.tsv");
        File iconsFile = new File(outputDir, "icons.zip");
        File errorFile = new File(outputDir, "error.txt");
        File progressFile = new File(outputDir, "progress.txt");
        File packageRequestFile = new File(outputDir, "packages.txt");
        File appPackageRequestFile = new File(appOutputDir(), "packages.txt");
        File deviceFile = new File(outputDir, "device.tsv");
        File deviceTemp = new File(outputDir, "device.tsv.tmp");
        File dataTemp = new File(outputDir, ".acbridge.tmp");
        File metadataTemp = new File(outputDir, "metadata.tsv.tmp");
        File iconsTemp = new File(outputDir, "icons.zip.tmp");
        dataFile.delete();
        metadataFile.delete();
        iconsFile.delete();
        errorFile.delete();
        progressFile.delete();
        deviceFile.delete();

        int labelCount = 0;
        int iconCount = 0;
        try {
            boolean rootGranted = false;
            if (rootMode) {
                rootGranted = rootAvailable();
                writeProgress(progressFile, "stage=root labels=0 icons=0 total=0 root=" + (rootGranted ? "1" : "0"));
            }
            PackageManager pm = getPackageManager();
            Set<String> requestedPackages = readRequestedPackages(packageRequestFile, appPackageRequestFile);
            List<PackageInfo> packages = pm.getInstalledPackages(0);
            List<PackageInfo> selectedPackages = new ArrayList<PackageInfo>(packages.size());
            for (PackageInfo info : packages) {
                if (info == null || info.packageName == null || info.applicationInfo == null) {
                    continue;
                }
                if (!requestedPackages.isEmpty() && !requestedPackages.contains(info.packageName)) {
                    continue;
                }
                selectedPackages.add(info);
            }
            Collections.sort(selectedPackages, new Comparator<PackageInfo>() {
                @Override
                public int compare(PackageInfo left, PackageInfo right) {
                    return left.packageName.compareToIgnoreCase(right.packageName);
                }
            });

            int total = selectedPackages.size();
            writeProgress(progressFile, "stage=labels labels=0 icons=0 total=" + total + " root=" + (rootGranted ? "1" : "0"));
            StringBuilder labels = new StringBuilder(Math.max(65536, total * 64));
            StringBuilder metadata = new StringBuilder(Math.max(65536, total * 64));
            for (PackageInfo info : selectedPackages) {
                if (info == null || info.packageName == null || info.applicationInfo == null) {
                    continue;
                }
                String label = info.packageName;
                try {
                    label = cleanLabel(String.valueOf(info.applicationInfo.loadLabel(pm)));
                    if (label.length() == 0) {
                        label = info.packageName;
                    }
                } catch (Throwable ignored) {
                    label = info.packageName;
                }
                labels.append(info.packageName).append(LABEL_SEPARATOR).append(label).append("|");
                metadata
                        .append(info.packageName)
                        .append('\t')
                        .append(cleanField(info.versionName))
                        .append('\t')
                        .append(versionCode(info))
                        .append('\t')
                        .append(appSizes ? apkSizeBytes(info.applicationInfo) : 0)
                        .append('\n');
                labelCount++;
                if ((labelCount & 63) == 0) {
                    writeProgress(progressFile, "stage=labels labels=" + labelCount + " icons=0 total=" + total + " root=" + (rootGranted ? "1" : "0"));
                }
            }

            writeText(dataTemp, labels.toString());
            writeText(metadataTemp, metadata.toString());
            writeText(deviceTemp, deviceSummary(outputDir));
            replace(dataTemp, dataFile);
            replace(metadataTemp, metadataFile);
            replace(deviceTemp, deviceFile);
            writeProgress(progressFile, "stage=icons labels=" + labelCount + " icons=0 total=" + total + " root=" + (rootGranted ? "1" : "0"));
            if (legacyMirror) {
                mirrorDataToLegacy(outputDir);
            }

            if (showIcons) {
                ZipOutputStream icons = new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(iconsTemp), 1048576));
                try {
                    for (PackageInfo info : selectedPackages) {
                        if (info == null || info.packageName == null || info.applicationInfo == null) {
                            continue;
                        }
                        try {
                            ApplicationInfo app = info.applicationInfo;
                            Drawable drawable;
                            try {
                                drawable = app.loadUnbadgedIcon(pm);
                            } catch (Throwable ignored) {
                                drawable = app.loadIcon(pm);
                            }
                            Bitmap bitmap = iconBitmap(drawable, iconSize);
                            if (bitmap != null) {
                                writeStoredPngEntry(icons, info.packageName + ".png", bitmap);
                                iconCount++;
                                if ((iconCount & 7) == 0) {
                                    writeProgress(progressFile, "stage=icons labels=" + labelCount + " icons=" + iconCount + " total=" + total + " root=" + (rootGranted ? "1" : "0"));
                                }
                            }
                        } catch (Throwable ignored) {
                        }
                    }
                } finally {
                    icons.close();
                }
                replace(iconsTemp, iconsFile);
                if (legacyMirror) {
                    mirrorIconsToLegacy(outputDir);
                }
            }
            writeProgress(progressFile, "stage=done labels=" + labelCount + " icons=" + iconCount + " total=" + total + " root=" + (rootGranted ? "1" : "0"));
            showStatus(
                    "OpenADB Bridge exported " + labelCount + " labels and " + iconCount + " icons."
                            + (rootMode ? " Root: " + (rootGranted ? "granted." : "not granted.") : ""),
                    endExit);
        } catch (Throwable exc) {
            String message = "OpenADB Bridge export failed: " + exc.getClass().getSimpleName() + ": " + exc.getMessage();
            try {
                writeText(errorFile, message);
            } catch (Exception ignored) {
            }
            showStatus(message, endExit);
        }
    }

    private void deletePath(File outputDir, Intent intent, Map<String, String> settings, boolean endExit) {
        File appDir = appOutputDir();
        if (!appDir.exists()) {
            appDir.mkdirs();
        }
        File resultFile = new File(outputDir, "delete_result.txt");
        File appResultFile = new File(appDir, "delete_result.txt");
        resultFile.delete();
        appResultFile.delete();

        String path = stringExtraOrSetting(intent, settings, "path", "");
        boolean recursive = booleanExtraOrSetting(intent, settings, "recursive", true);
        boolean rootMode = booleanExtraOrSetting(intent, settings, "rootmode", false);
        List<String> notes = new ArrayList<String>();
        if (path == null || path.trim().length() == 0) {
            writeDeleteResult(resultFile, appResultFile, false, "No Android path was provided.");
            showStatus("OpenADB Bridge delete failed: no path.", endExit);
            return;
        }

        File target = new File(path);
        boolean removablePath = isPublicRemovableStoragePath(path);
        boolean deletionMethodWorked = false;
        boolean rootGranted = false;
        if (rootMode) {
            rootGranted = rootAvailable();
            notes.add(rootGranted ? "root granted" : "root not granted");
            if (rootGranted) {
                CommandOutcome root = runCommand(new String[] {"su", "-c", "rm " + (recursive ? "-rf " : "-f ") + shellQuote(path)});
                notes.add("root rm exit=" + root.exitCode + " " + root.output);
                deletionMethodWorked = root.exitCode == 0;
            }
        }

        if (target.exists() || removablePath) {
            int deletedRows = deleteViaMediaStore(path, recursive, notes);
            notes.add("mediastore rows=" + deletedRows);
            deletionMethodWorked = deletionMethodWorked || deletedRows > 0;
        }
        if (target.exists() || removablePath) {
            boolean saf = deleteViaSaf(path, recursive, notes);
            notes.add("saf=" + saf);
            deletionMethodWorked = deletionMethodWorked || saf;
        }
        if (target.exists()) {
            boolean fileApi = deleteViaFileApi(target, recursive, notes);
            notes.add("file api=" + fileApi);
            deletionMethodWorked = deletionMethodWorked || fileApi;
        }
        if (target.exists() || (removablePath && !deletionMethodWorked)) {
            int deletedRows = deleteViaMediaStore(path, recursive, notes);
            notes.add("mediastore retry rows=" + deletedRows);
            deletionMethodWorked = deletionMethodWorked || deletedRows > 0;
        }
        if (target.exists()) {
            deleteEmptyDirectories(target, notes);
        }
        scanAfterDelete(path);

        boolean success = !target.exists();
        if (removablePath && !deletionMethodWorked) {
            success = false;
        }
        String detail = (success ? "Deleted through ACBridge: " : "ACBridge could not delete: ") + path;
        if (!notes.isEmpty()) {
            detail += " (" + joinNotes(notes) + ")";
        }
        writeDeleteResult(resultFile, appResultFile, success, detail);
        showStatus(detail, endExit);
    }

    private void requestStorageAccess(Intent intent) {
        pendingGrantPath = intent.getStringExtra("path");
        if (pendingGrantPath == null) {
            pendingGrantPath = "";
        }
        pendingGrantEndExit = intent.getBooleanExtra("endexit", true);
        storageGrantPending = true;
        storageGrantAttempts = 0;
        status.setText(storageGrantInstructionText("OpenADB Bridge needs Android TV storage access."));
        status.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                openRequiredStorageAccess();
            }
        });
        status.setOnKeyListener(new View.OnKeyListener() {
            @Override
            public boolean onKey(View view, int keyCode, KeyEvent event) {
                if (event == null || event.getAction() != KeyEvent.ACTION_UP) {
                    return false;
                }
                if (keyCode == KeyEvent.KEYCODE_DPAD_CENTER
                        || keyCode == KeyEvent.KEYCODE_ENTER
                        || keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER) {
                    openRequiredStorageAccess();
                    return true;
                }
                return false;
            }
        });
        status.requestFocus();
        status.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (storageGrantPending) {
                    openRequiredStorageAccess();
                }
            }
        }, 500);
    }

    private void openRequiredStorageAccess() {
        if (Build.VERSION.SDK_INT >= 30 && isInternalSharedStoragePath(pendingGrantPath)) {
            requestAllFilesAccess(new SecurityException("Internal shared storage root requires All files access"));
            return;
        }
        openStorageTreePicker();
    }

    private void openStorageTreePicker() {
        storageGrantAttempts++;
        Intent request = storageTreeIntentForPath(pendingGrantPath);
        try {
            startActivityForResult(request, REQUEST_STORAGE_TREE);
        } catch (Throwable exc) {
            requestAllFilesAccess(exc);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_ALL_FILES_ACCESS) {
            finishAllFilesAccessRequest();
            return;
        }
        if (requestCode != REQUEST_STORAGE_TREE) {
            return;
        }
        File resultFile = new File(outputDir(), "delete_result.txt");
        File appResultFile = new File(appOutputDir(), "delete_result.txt");
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            status.setText(storageGrantInstructionText(
                    "Storage access was not granted. Press OK/Enter to open the picker again, or Back to cancel."
            ));
            status.requestFocus();
            return;
        }
        Uri treeUri = data.getData();
        String treeId = "";
        try {
            treeId = DocumentsContract.getTreeDocumentId(treeUri);
        } catch (Throwable ignored) {
        }
        if (!selectedTreeCoversPendingPath(treeId)) {
            status.setText(storageGrantInstructionText(
                    "The selected storage does not match the target path. Select the MicroSD/USB root shown below."
            ));
            status.requestFocus();
            return;
        }
        int flags = data.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        if (flags == 0) {
            flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
        }
        try {
            getContentResolver().takePersistableUriPermission(treeUri, flags);
        } catch (Throwable ignored) {
        }
        try {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_LAST_TREE_URI, treeUri.toString()).apply();
        } catch (Throwable ignored) {
        }
        String message = "SAF_PERMISSION_GRANTED\tStorage access granted for " + treeId + ". OpenADB can continue.";
        writeDeleteResult(resultFile, appResultFile, true, message);
        storageGrantPending = false;
        pendingGrantPath = "";
        showStatus(message, pendingGrantEndExit);
    }

    @Override
    public void onBackPressed() {
        if (storageGrantPending) {
            String message = "SAF_PERMISSION_DENIED\tStorage access was cancelled by the user. OpenADB cannot access this MicroSD/USB path without this Android permission.";
            writeDeleteResult(new File(outputDir(), "delete_result.txt"), new File(appOutputDir(), "delete_result.txt"), false, message);
            storageGrantPending = false;
            pendingGrantPath = "";
            showStatus(message, pendingGrantEndExit);
            return;
        }
        super.onBackPressed();
    }

    private String storageGrantInstructionText(String firstLine) {
        String storageId = storageIdFromPath(pendingGrantPath);
        String volumeText = storageId.length() > 0 ? storageId : "the MicroSD/USB storage";
        return firstLine
                + "\n\nTarget path:\n" + pendingGrantPath
                + "\n\nOn the TV screen select the ROOT of this storage volume:"
                + "\n" + volumeText
                + "\n\nThen press Select/Use this folder/Allow."
                + "\nIf the picker closes, press OK/Enter here to try again."
                + "\nPress Back here only if you want to cancel."
                + "\n\nAttempts: " + storageGrantAttempts;
    }

    private void requestAllFilesAccess(Throwable pickerFailure) {
        if (Build.VERSION.SDK_INT < 30) {
            String message = "SAF_PERMISSION_FAILED\tAndroid could not open the storage picker and this Android version has no All files access settings: "
                    + pickerFailure.getClass().getSimpleName() + ": " + pickerFailure.getMessage();
            writeDeleteResult(new File(outputDir(), "delete_result.txt"), new File(appOutputDir(), "delete_result.txt"), false, message);
            storageGrantPending = false;
            showStatus(message, pendingGrantEndExit);
            return;
        }
        if (Environment.isExternalStorageManager()) {
            finishAllFilesAccessRequest();
            return;
        }
        status.setText("OpenADB Bridge needs broad storage access for this selected location.\n\n"
                + "OpenADB Bridge will open Android settings now.\n"
                + "Enable All files access / Allow access to manage all files for OpenADB Bridge, then return here.\n\n"
                + "Target path:\n" + pendingGrantPath);
        status.requestFocus();
        try {
            Intent request = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
            request.setData(Uri.parse("package:" + getPackageName()));
            startActivityForResult(request, REQUEST_ALL_FILES_ACCESS);
        } catch (Throwable appSettingsFailure) {
            try {
                Intent request = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                startActivityForResult(request, REQUEST_ALL_FILES_ACCESS);
            } catch (Throwable globalSettingsFailure) {
                String message = "SAF_PERMISSION_FAILED\tAndroid could not open All files access settings: "
                        + pickerFailure.getClass().getSimpleName() + ": " + pickerFailure.getMessage()
                        + "; settings: " + globalSettingsFailure.getClass().getSimpleName() + ": " + globalSettingsFailure.getMessage();
                writeDeleteResult(new File(outputDir(), "delete_result.txt"), new File(appOutputDir(), "delete_result.txt"), false, message);
                storageGrantPending = false;
                showStatus(message, pendingGrantEndExit);
            }
        }
    }

    private void finishAllFilesAccessRequest() {
        File resultFile = new File(outputDir(), "delete_result.txt");
        File appResultFile = new File(appOutputDir(), "delete_result.txt");
        if (Build.VERSION.SDK_INT >= 30 && Environment.isExternalStorageManager()) {
            String message = "ALL_FILES_PERMISSION_GRANTED\tAll files access is enabled for ACBridge. OpenADB can continue.";
            writeDeleteResult(resultFile, appResultFile, true, message);
            storageGrantPending = false;
            pendingGrantPath = "";
            showStatus(message, pendingGrantEndExit);
            return;
        }
        String message = "ALL_FILES_PERMISSION_DENIED\tAll files access is not enabled for ACBridge. OpenADB cannot access this selected storage path without Android storage permission.";
        writeDeleteResult(resultFile, appResultFile, false, message);
        storageGrantPending = false;
        showStatus(message, pendingGrantEndExit);
    }

    private boolean selectedTreeCoversPendingPath(String treeId) {
        String storageId = storageIdFromPath(pendingGrantPath);
        if (storageId.length() == 0 || treeId == null || treeId.length() == 0) {
            return true;
        }
        String treeVolume = volumeFromDocumentId(treeId);
        if (!storageMatchesTree(storageId, treeVolume)) {
            return false;
        }
        String treeRelative = relativeFromDocumentId(treeId);
        if (treeRelative.length() == 0) {
            return true;
        }
        String relative = relativePathFromStoragePath(pendingGrantPath);
        if (relative.length() == 0) {
            return true;
        }
        return relative.equals(treeRelative) || relative.startsWith(treeRelative + "/");
    }

    private int deleteViaMediaStore(String path, boolean recursive, List<String> notes) {
        int total = 0;
        ContentResolver resolver = getContentResolver();
        String clean = trimTrailingSlash(path);
        String selection = recursive ? "(_data=? OR _data LIKE ?)" : "_data=?";
        String[] args = recursive ? new String[] {clean, clean + "/%"} : new String[] {clean};
        for (String volume : mediaStoreVolumesForPath(clean)) {
            try {
                Uri uri = MediaStore.Files.getContentUri(volume);
                int deleted = resolver.delete(uri, selection, args);
                total += Math.max(0, deleted);
                notes.add("volume " + volume + " deleted=" + deleted);
            } catch (Throwable exc) {
                notes.add("volume " + volume + " failed=" + exc.getClass().getSimpleName());
            }
        }
        return total;
    }

    private boolean deleteViaSaf(String path, boolean recursive, List<String> notes) {
        String clean = trimTrailingSlash(path);
        boolean hadTree = false;
        for (Uri treeUri : persistedTreeUris()) {
            hadTree = true;
            String traversedDocumentId = findDocumentIdByTreeTraversal(treeUri, clean, notes);
            if (traversedDocumentId.length() > 0) {
                try {
                    Uri documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, traversedDocumentId);
                    if (deleteDocumentUri(treeUri, documentUri, traversedDocumentId, recursive, notes)) {
                        notes.add("saf tree deleted " + traversedDocumentId);
                        return true;
                    }
                } catch (Throwable exc) {
                    notes.add("saf tree failed " + traversedDocumentId + "=" + exc.getClass().getSimpleName());
                }
            }
            List<String> documentIds = documentIdsForPath(treeUri, clean, notes);
            if (documentIds.isEmpty()) {
                continue;
            }
            for (String documentId : documentIds) {
                try {
                    Uri documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, documentId);
                    if (deleteDocumentUri(treeUri, documentUri, documentId, recursive, notes)) {
                        notes.add("saf deleted " + documentId);
                        return true;
                    }
                } catch (Throwable exc) {
                    notes.add("saf failed " + documentId + "=" + exc.getClass().getSimpleName());
                }
            }
        }
        notes.add(hadTree ? "SAF_GRANTED_BUT_PATH_NOT_DELETED" : "SAF_PERMISSION_REQUIRED: grant MicroSD/USB access on the TV");
        return false;
    }

    private String findDocumentIdByTreeTraversal(Uri treeUri, String path, List<String> notes) {
        String treeId;
        try {
            treeId = DocumentsContract.getTreeDocumentId(treeUri);
        } catch (Throwable exc) {
            notes.add("saf traversal bad tree=" + exc.getClass().getSimpleName());
            return "";
        }
        String storageId = storageIdFromPath(path);
        String relative = relativePathFromStoragePath(path);
        if (storageId.length() == 0 || relative.length() == 0) {
            return "";
        }
        String treeVolume = volumeFromDocumentId(treeId);
        if (!storageMatchesTree(storageId, treeVolume)) {
            return "";
        }
        String relativeWithinTree = relative;
        String treeRelative = relativeFromDocumentId(treeId);
        if (treeRelative.length() > 0) {
            if (relative.equals(treeRelative)) {
                return treeId;
            }
            String prefix = treeRelative.endsWith("/") ? treeRelative : treeRelative + "/";
            if (!relative.startsWith(prefix)) {
                return "";
            }
            relativeWithinTree = relative.substring(prefix.length());
        }
        String currentDocumentId = treeId;
        List<String> components = pathComponents(relativeWithinTree);
        for (String component : components) {
            String nextDocumentId = findChildDocumentId(treeUri, currentDocumentId, component, notes);
            if (nextDocumentId.length() == 0) {
                return "";
            }
            currentDocumentId = nextDocumentId;
        }
        return currentDocumentId;
    }

    private List<String> pathComponents(String relativePath) {
        ArrayList<String> components = new ArrayList<String>();
        if (relativePath == null || relativePath.length() == 0) {
            return components;
        }
        String[] parts = relativePath.split("/");
        for (String part : parts) {
            if (part != null && part.length() > 0) {
                components.add(part);
            }
        }
        return components;
    }

    private String findChildDocumentId(Uri treeUri, String parentDocumentId, String displayName, List<String> notes) {
        Cursor cursor = null;
        try {
            Uri childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocumentId);
            cursor = getContentResolver().query(
                    childrenUri,
                    new String[] {Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME},
                    null,
                    null,
                    null
            );
            if (cursor != null) {
                while (cursor.moveToNext()) {
                    String childId = cursor.getString(0);
                    String childName = cursor.getString(1);
                    if (displayName.equals(childName) && childId != null && childId.length() > 0) {
                        return childId;
                    }
                }
            }
        } catch (Throwable exc) {
            notes.add("saf traversal failed at " + displayName + "=" + exc.getClass().getSimpleName());
        } finally {
            if (cursor != null) {
                cursor.close();
            }
        }
        notes.add("saf child not found " + displayName);
        return "";
    }

    private List<Uri> persistedTreeUris() {
        ArrayList<Uri> uris = new ArrayList<Uri>();
        try {
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            String last = prefs.getString(PREF_LAST_TREE_URI, "");
            if (last != null && last.length() > 0) {
                uris.add(Uri.parse(last));
            }
        } catch (Throwable ignored) {
        }
        try {
            List<UriPermission> permissions = getContentResolver().getPersistedUriPermissions();
            for (UriPermission permission : permissions) {
                if (permission == null || !permission.isWritePermission()) {
                    continue;
                }
                Uri uri = permission.getUri();
                if (uri != null && !containsUri(uris, uri)) {
                    uris.add(uri);
                }
            }
        } catch (Throwable ignored) {
        }
        return uris;
    }

    private boolean containsUri(List<Uri> uris, Uri uri) {
        String text = uri.toString();
        for (Uri existing : uris) {
            if (text.equals(existing.toString())) {
                return true;
            }
        }
        return false;
    }

    private List<String> documentIdsForPath(Uri treeUri, String path, List<String> notes) {
        ArrayList<String> ids = new ArrayList<String>();
        String treeId;
        try {
            treeId = DocumentsContract.getTreeDocumentId(treeUri);
        } catch (Throwable exc) {
            notes.add("saf bad tree=" + exc.getClass().getSimpleName());
            return ids;
        }
        String treeVolume = volumeFromDocumentId(treeId);
        String treeRelative = relativeFromDocumentId(treeId);
        String storageId = storageIdFromPath(path);
        String relative = relativePathFromStoragePath(path);
        if (storageId.length() == 0) {
            return ids;
        }
        if (!storageMatchesTree(storageId, treeVolume)) {
            return ids;
        }
        if (treeRelative.length() > 0) {
            if (relative.equals(treeRelative)) {
                ids.add(treeId);
                return ids;
            }
            String prefix = treeRelative.endsWith("/") ? treeRelative : treeRelative + "/";
            if (!relative.startsWith(prefix)) {
                return ids;
            }
        }
        String docVolume = treeVolume.length() > 0 ? treeVolume : storageId;
        ids.add(docVolume + ":" + relative);
        if (!storageId.equals(docVolume)) {
            ids.add(storageId + ":" + relative);
        }
        for (String variant : storageVariants(storageId)) {
            String candidate = variant + ":" + relative;
            if (!ids.contains(candidate)) {
                ids.add(candidate);
            }
        }
        return ids;
    }

    private boolean deleteDocumentUri(Uri treeUri, Uri documentUri, String documentId, boolean recursive, List<String> notes) {
        String mime = documentMimeType(documentUri);
        if (Document.MIME_TYPE_DIR.equals(mime)) {
            if (!recursive) {
                return deleteSingleDocument(documentUri, notes);
            }
            Uri childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, documentId);
            Cursor cursor = null;
            try {
                cursor = getContentResolver().query(childrenUri, new String[] {Document.COLUMN_DOCUMENT_ID}, null, null, null);
                if (cursor != null) {
                    while (cursor.moveToNext()) {
                        String childId = cursor.getString(0);
                        if (childId == null || childId.length() == 0) {
                            continue;
                        }
                        Uri childUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, childId);
                        deleteDocumentUri(treeUri, childUri, childId, true, notes);
                    }
                }
            } catch (Throwable exc) {
                notes.add("saf list failed=" + exc.getClass().getSimpleName());
            } finally {
                if (cursor != null) {
                    cursor.close();
                }
            }
        }
        return deleteSingleDocument(documentUri, notes);
    }

    private boolean deleteSingleDocument(Uri documentUri, List<String> notes) {
        try {
            return DocumentsContract.deleteDocument(getContentResolver(), documentUri);
        } catch (Throwable exc) {
            notes.add("saf delete failed=" + exc.getClass().getSimpleName());
            return false;
        }
    }

    private String documentMimeType(Uri documentUri) {
        Cursor cursor = null;
        try {
            cursor = getContentResolver().query(documentUri, new String[] {Document.COLUMN_MIME_TYPE}, null, null, null);
            if (cursor != null && cursor.moveToFirst()) {
                String value = cursor.getString(0);
                return value == null ? "" : value;
            }
        } catch (Throwable ignored) {
        } finally {
            if (cursor != null) {
                cursor.close();
            }
        }
        return "";
    }

    private Set<String> mediaStoreVolumesForPath(String path) {
        LinkedHashSet<String> volumes = new LinkedHashSet<String>();
        String storageId = storageIdFromPath(path);
        if (storageId.length() > 0) {
            addStorageVariants(volumes, storageId);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                Set<String> externalVolumes = MediaStore.getExternalVolumeNames(this);
                for (String volume : externalVolumes) {
                    if (volume != null && volume.length() > 0) {
                        volumes.add(volume);
                    }
                }
            } catch (Throwable ignored) {
            }
        }
        volumes.add("external_primary");
        volumes.add("external");
        return volumes;
    }

    private Intent storageTreeIntentForPath(String path) {
        Intent request = null;
        String storageId = storageIdFromPath(path);
        if (Build.VERSION.SDK_INT >= 29 && storageId.length() > 0) {
            try {
                StorageManager manager = getSystemService(StorageManager.class);
                if (manager != null) {
                    List<StorageVolume> volumes = manager.getStorageVolumes();
                    for (StorageVolume volume : volumes) {
                        if (volume != null && storageVolumeMatchesPath(volume, storageId, path)) {
                            request = volume.createOpenDocumentTreeIntent();
                            break;
                        }
                    }
                }
            } catch (Throwable ignored) {
            }
        }
        if (request == null) {
            request = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        }
        request.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        request.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        request.addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        request.addFlags(Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
        request.putExtra("android.content.extra.SHOW_ADVANCED", true);
        request.putExtra("android.provider.extra.SHOW_ADVANCED", true);
        return request;
    }

    private boolean storageVolumeMatchesPath(StorageVolume volume, String storageId, String path) {
        try {
            String uuid = volume.getUuid();
            if (uuid != null && uuid.length() > 0 && storageMatchesTree(storageId, uuid)) {
                return true;
            }
        } catch (Throwable ignored) {
        }
        try {
            if (volume.isPrimary()) {
                return "primary".equalsIgnoreCase(storageId) || "emulated".equalsIgnoreCase(storageId);
            }
        } catch (Throwable ignored) {
        }
        try {
            java.lang.reflect.Method method = volume.getClass().getMethod("getDirectory");
            Object value = method.invoke(volume);
            if (value instanceof File) {
                String volumePath = trimTrailingSlash(((File) value).getAbsolutePath());
                String cleanPath = trimTrailingSlash(path);
                if (volumePath.length() > 0 && cleanPath.startsWith(volumePath + "/")) {
                    return true;
                }
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    private String storageIdFromPath(String path) {
        String clean = path == null ? "" : path.replace('\\', '/');
        String prefix = "/storage/";
        if (!clean.startsWith(prefix)) {
            return "";
        }
        int start = prefix.length();
        int end = clean.indexOf('/', start);
        String storageId = end >= 0 ? clean.substring(start, end) : clean.substring(start);
        if ("emulated".equals(storageId) || "self".equals(storageId)) {
            return "";
        }
        return storageId;
    }

    private boolean isPublicRemovableStoragePath(String path) {
        String clean = path == null ? "" : path.replace('\\', '/');
        return clean.startsWith("/storage/")
                && !clean.startsWith("/storage/emulated/")
                && !clean.startsWith("/storage/self/");
    }

    private boolean isInternalSharedStoragePath(String path) {
        String clean = trimTrailingSlash(path);
        return clean.equals("/sdcard")
                || clean.startsWith("/sdcard/")
                || clean.equals("/storage/emulated/0")
                || clean.startsWith("/storage/emulated/0/")
                || clean.equals("/storage/self/primary")
                || clean.startsWith("/storage/self/primary/");
    }

    private String relativePathFromStoragePath(String path) {
        String clean = trimTrailingSlash(path);
        String prefix = "/storage/";
        if (!clean.startsWith(prefix)) {
            return "";
        }
        int start = prefix.length();
        int slash = clean.indexOf('/', start);
        if (slash < 0 || slash + 1 >= clean.length()) {
            return "";
        }
        return clean.substring(slash + 1);
    }

    private String volumeFromDocumentId(String documentId) {
        if (documentId == null) {
            return "";
        }
        int separator = documentId.indexOf(':');
        return separator >= 0 ? documentId.substring(0, separator) : documentId;
    }

    private String relativeFromDocumentId(String documentId) {
        if (documentId == null) {
            return "";
        }
        int separator = documentId.indexOf(':');
        if (separator < 0 || separator + 1 >= documentId.length()) {
            return "";
        }
        return documentId.substring(separator + 1);
    }

    private boolean storageMatchesTree(String storageId, String treeVolume) {
        if (storageId == null || treeVolume == null || storageId.length() == 0 || treeVolume.length() == 0) {
            return false;
        }
        String left = storageId.toLowerCase();
        String right = treeVolume.toLowerCase();
        if (left.equals(right)) {
            return true;
        }
        for (String variant : storageVariants(storageId)) {
            if (right.equals(variant.toLowerCase())) {
                return true;
            }
        }
        String compactLeft = storageId.replaceAll("[^0-9A-Fa-f]", "").toLowerCase();
        String compactRight = treeVolume.replaceAll("[^0-9A-Fa-f]", "").toLowerCase();
        if (compactLeft.length() > 0 && compactRight.length() > 0) {
            return compactLeft.startsWith(compactRight) || compactRight.startsWith(compactLeft);
        }
        return !"primary".equals(right) && !"home".equals(right);
    }

    private List<String> storageVariants(String storageId) {
        ArrayList<String> variants = new ArrayList<String>();
        LinkedHashSet<String> set = new LinkedHashSet<String>();
        addStorageVariants(set, storageId);
        variants.addAll(set);
        return variants;
    }

    private void addStorageVariants(Set<String> volumes, String storageId) {
        volumes.add(storageId);
        volumes.add(storageId.toLowerCase());
        volumes.add(storageId.toUpperCase());
        String compact = storageId.replaceAll("[^0-9A-Fa-f]", "");
        if (compact.length() == 8) {
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(4)).toUpperCase());
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(4)).toLowerCase());
        } else if (compact.length() == 16) {
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(4, 8)).toUpperCase());
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(4, 8)).toLowerCase());
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(12)).toUpperCase());
            volumes.add((compact.substring(0, 4) + "-" + compact.substring(12)).toLowerCase());
        }
    }

    private boolean deleteViaFileApi(File target, boolean recursive, List<String> notes) {
        if (!target.exists()) {
            return true;
        }
        if (target.isDirectory()) {
            if (!recursive) {
                return target.delete();
            }
            File[] children = target.listFiles();
            if (children == null) {
                notes.add("listFiles returned null for " + target.getAbsolutePath());
            } else {
                for (File child : children) {
                    deleteViaFileApi(child, true, notes);
                }
            }
        }
        return target.delete() || !target.exists();
    }

    private void deleteEmptyDirectories(File target, List<String> notes) {
        if (!target.exists() || !target.isDirectory()) {
            return;
        }
        File[] children = target.listFiles();
        if (children != null) {
            for (File child : children) {
                deleteEmptyDirectories(child, notes);
            }
        }
        if (target.delete()) {
            notes.add("removed empty dir " + target.getName());
        }
    }

    private void scanAfterDelete(String path) {
        try {
            String parent = new File(path).getParent();
            if (parent != null) {
                MediaScannerConnection.scanFile(this, new String[] {path, parent}, null, null);
            } else {
                MediaScannerConnection.scanFile(this, new String[] {path}, null, null);
            }
        } catch (Throwable ignored) {
        }
    }

    private void writeDeleteResult(File resultFile, File appResultFile, boolean success, String message) {
        String text = (success ? "OK\t" : "ERROR\t") + cleanField(message);
        try {
            writeText(resultFile, text);
        } catch (Exception ignored) {
        }
        try {
            writeText(appResultFile, text);
        } catch (Exception ignored) {
        }
    }

    private String trimTrailingSlash(String path) {
        if (path == null) {
            return "";
        }
        String clean = path.replace('\\', '/');
        while (clean.length() > 1 && clean.endsWith("/")) {
            clean = clean.substring(0, clean.length() - 1);
        }
        return clean;
    }

    private String shellQuote(String value) {
        return "'" + String.valueOf(value).replace("'", "'\\''") + "'";
    }

    private CommandOutcome runCommand(String[] command) {
        Process process = null;
        BufferedReader reader = null;
        StringBuilder output = new StringBuilder();
        try {
            process = new ProcessBuilder(command).redirectErrorStream(true).start();
            reader = new BufferedReader(new java.io.InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                if (output.length() < 1200) {
                    output.append(line).append(' ');
                }
            }
            return new CommandOutcome(process.waitFor(), output.toString().trim());
        } catch (Throwable exc) {
            return new CommandOutcome(1, exc.getClass().getSimpleName() + ": " + exc.getMessage());
        } finally {
            if (reader != null) {
                try {
                    reader.close();
                } catch (Exception ignored) {
                }
            }
            if (process != null) {
                process.destroy();
            }
        }
    }

    private String joinNotes(List<String> notes) {
        StringBuilder builder = new StringBuilder();
        for (String note : notes) {
            if (note == null || note.length() == 0) {
                continue;
            }
            if (builder.length() > 0) {
                builder.append("; ");
            }
            builder.append(note);
            if (builder.length() > 1800) {
                builder.append("...");
                break;
            }
        }
        return builder.toString();
    }

    private File outputDir() {
        File publicDir = new File(Environment.getExternalStorageDirectory(), ".adac");
        File appDir = appOutputDir();
        if (isAndroidTv() && ensureDirectory(appDir)) {
            return appDir;
        }
        if (ensureDirectory(publicDir)) {
            return publicDir;
        }
        if (ensureDirectory(appDir)) {
            return appDir;
        }
        return publicDir;
    }

    private File appOutputDir() {
        File external = getExternalFilesDir(null);
        if (external != null) {
            return new File(external, "openadb");
        }
        return new File(Environment.getExternalStorageDirectory(), ".adac");
    }

    private boolean isAndroidTv() {
        try {
            UiModeManager uiMode = (UiModeManager) getSystemService(UI_MODE_SERVICE);
            if (uiMode != null && uiMode.getCurrentModeType() == Configuration.UI_MODE_TYPE_TELEVISION) {
                return true;
            }
        } catch (Throwable ignored) {
        }
        try {
            PackageManager pm = getPackageManager();
            return pm.hasSystemFeature(PackageManager.FEATURE_LEANBACK)
                    || pm.hasSystemFeature("android.software.leanback_only");
        } catch (Throwable ignored) {
            return false;
        }
    }

    private String deviceSummary(File outputDir) {
        StringBuilder builder = new StringBuilder();
        builder.append("formFactor\t").append(isAndroidTv() ? "Android TV" : "Android").append('\n');
        builder.append("sdk\t").append(Build.VERSION.SDK_INT).append('\n');
        builder.append("outputDir\t").append(cleanField(outputDir.getAbsolutePath())).append('\n');
        builder.append("externalStorage\t").append(cleanField(Environment.getExternalStorageDirectory().getAbsolutePath())).append('\n');
        builder.append("externalStorageState\t").append(cleanField(Environment.getExternalStorageState())).append('\n');
        File appDir = appOutputDir();
        builder.append("appOutputDir\t").append(cleanField(appDir.getAbsolutePath())).append('\n');
        return builder.toString();
    }

    private boolean ensureDirectory(File directory) {
        if (!directory.exists() && !directory.mkdirs()) {
            return false;
        }
        File probe = new File(directory, ".openadb_probe");
        try {
            writeText(probe, "ok");
            probe.delete();
            return true;
        } catch (Exception ignored) {
            probe.delete();
            return false;
        }
    }

    private void mirrorDataToLegacy(File sourceDir) {
        File legacy = new File(Environment.getExternalStorageDirectory(), ".adac");
        if (samePath(sourceDir, legacy)) {
            return;
        }
        if (!legacy.exists() && !legacy.mkdirs()) {
            return;
        }
        copyFile(new File(sourceDir, ".acbridge"), new File(legacy, ".acbridge"));
    }

    private void mirrorIconsToLegacy(File sourceDir) {
        File legacy = new File(Environment.getExternalStorageDirectory(), ".adac");
        if (samePath(sourceDir, legacy)) {
            return;
        }
        if (!legacy.exists() && !legacy.mkdirs()) {
            return;
        }
        copyFile(new File(sourceDir, "icons.zip"), new File(legacy, "icons.zip"));
    }

    private boolean samePath(File left, File right) {
        try {
            return left.getCanonicalPath().equals(right.getCanonicalPath());
        } catch (Exception exc) {
            return left.getAbsolutePath().equals(right.getAbsolutePath());
        }
    }

    private void copyFile(File source, File target) {
        if (!source.isFile()) {
            return;
        }
        File temp = new File(target.getParentFile(), target.getName() + ".tmp");
        try {
            copyBytes(source, temp);
            replace(temp, target);
        } catch (Exception ignored) {
        }
    }

    private void replace(File temp, File target) {
        if (target.exists()) {
            target.delete();
        }
        if (!temp.renameTo(target)) {
            try {
                copyBytes(temp, target);
            } catch (Exception ignored) {
            }
            temp.delete();
        }
    }

    private void copyBytes(File source, File target) throws java.io.IOException {
        byte[] buffer = new byte[262144];
        java.io.FileInputStream input = new java.io.FileInputStream(source);
        java.io.FileOutputStream output = new java.io.FileOutputStream(target);
        try {
            int read;
            while ((read = input.read(buffer)) != -1) {
                output.write(buffer, 0, read);
            }
        } finally {
            input.close();
            output.close();
        }
    }

    private void writeText(File target, String text) throws java.io.IOException {
        OutputStreamWriter writer = new OutputStreamWriter(new FileOutputStream(target), StandardCharsets.UTF_8);
        try {
            writer.write(text);
        } finally {
            writer.close();
        }
    }

    private void writeProgress(File target, String text) {
        try {
            writeText(target, text);
        } catch (Exception ignored) {
        }
    }

    private boolean rootAvailable() {
        Process process = null;
        BufferedReader reader = null;
        try {
            process = new ProcessBuilder("su", "-c", "id -u").redirectErrorStream(true).start();
            reader = new BufferedReader(new java.io.InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                if ("0".equals(line.trim())) {
                    return process.waitFor() == 0;
                }
            }
            return process.waitFor() == 0;
        } catch (Throwable ignored) {
            return false;
        } finally {
            if (reader != null) {
                try {
                    reader.close();
                } catch (Exception ignored) {
                }
            }
            if (process != null) {
                process.destroy();
            }
        }
    }

    private Set<String> readRequestedPackages(File... targets) {
        Set<String> packages = new HashSet<String>();
        if (targets == null) {
            return packages;
        }
        for (File target : targets) {
            if (target == null || !target.isFile()) {
                continue;
            }
            BufferedReader reader = null;
            try {
                reader = new BufferedReader(new FileReader(target));
                String line;
                while ((line = reader.readLine()) != null) {
                    line = line.trim();
                    if (line.length() > 0) {
                        packages.add(line);
                    }
                }
            } catch (Exception ignored) {
            } finally {
                if (reader != null) {
                    try {
                        reader.close();
                    } catch (Exception ignored) {
                    }
                }
            }
        }
        return packages;
    }

    private Map<String, String> readSettings(File target) {
        Map<String, String> settings = new HashMap<String, String>();
        if (target == null || !target.isFile()) {
            return settings;
        }
        BufferedReader reader = null;
        try {
            reader = new BufferedReader(new FileReader(target));
            String line;
            while ((line = reader.readLine()) != null) {
                int separator = line.indexOf('=');
                if (separator <= 0) {
                    continue;
                }
                settings.put(line.substring(0, separator).trim().toLowerCase(), line.substring(separator + 1).trim());
            }
        } catch (Exception ignored) {
        } finally {
            if (reader != null) {
                try {
                    reader.close();
                } catch (Exception ignored) {
                }
            }
        }
        return settings;
    }

    private boolean booleanExtraOrSetting(Intent intent, Map<String, String> settings, String key, boolean defaultValue) {
        if (intent != null && intent.hasExtra(key)) {
            return intent.getBooleanExtra(key, defaultValue);
        }
        String value = settings.get(key.toLowerCase());
        if (value == null) {
            return defaultValue;
        }
        return "true".equalsIgnoreCase(value) || "1".equals(value) || "yes".equalsIgnoreCase(value);
    }

    private String stringExtraOrSetting(Intent intent, Map<String, String> settings, String key, String defaultValue) {
        if (intent != null && intent.hasExtra(key)) {
            String value = intent.getStringExtra(key);
            return value == null ? defaultValue : value;
        }
        String value = settings.get(key.toLowerCase());
        return value == null ? defaultValue : value;
    }

    private int intExtraOrSetting(Intent intent, Map<String, String> settings, String key, int defaultValue) {
        if (intent != null && intent.hasExtra(key)) {
            return intent.getIntExtra(key, defaultValue);
        }
        String value = settings.get(key.toLowerCase());
        if (value == null) {
            return defaultValue;
        }
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException ignored) {
            return defaultValue;
        }
    }

    private void writeStoredPngEntry(ZipOutputStream zip, String name, Bitmap bitmap) throws java.io.IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(65536);
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, bytes);
        byte[] data = bytes.toByteArray();
        CRC32 crc = new CRC32();
        crc.update(data);
        ZipEntry entry = new ZipEntry(name);
        entry.setMethod(ZipEntry.STORED);
        entry.setSize(data.length);
        entry.setCompressedSize(data.length);
        entry.setCrc(crc.getValue());
        zip.putNextEntry(entry);
        zip.write(data);
        zip.closeEntry();
    }

    private String cleanLabel(String value) {
        if (value == null) {
            return "";
        }
        return value.replace('|', ' ').replace('\n', ' ').replace('\r', ' ').trim();
    }

    private String cleanField(String value) {
        if (value == null) {
            return "";
        }
        return value.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').trim();
    }

    private long versionCode(PackageInfo info) {
        if (Build.VERSION.SDK_INT >= 28) {
            return info.getLongVersionCode();
        }
        return info.versionCode;
    }

    private long apkSizeBytes(ApplicationInfo info) {
        if (info == null) {
            return 0;
        }
        long total = fileSize(info.sourceDir);
        if (info.splitSourceDirs != null) {
            for (String path : info.splitSourceDirs) {
                total += fileSize(path);
            }
        }
        return Math.max(0, total);
    }

    private long fileSize(String path) {
        if (path == null || path.length() == 0) {
            return 0;
        }
        try {
            File file = new File(path);
            return file.isFile() ? Math.max(0, file.length()) : 0;
        } catch (Throwable ignored) {
            return 0;
        }
    }

    private Bitmap iconBitmap(Drawable drawable, int size) {
        if (drawable == null) {
            return null;
        }
        if (drawable instanceof BitmapDrawable) {
            Bitmap source = ((BitmapDrawable) drawable).getBitmap();
            if (source != null) {
                return Bitmap.createScaledBitmap(source, size, size, true);
            }
        }

        Bitmap bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        canvas.drawColor(Color.TRANSPARENT);

        drawable.setBounds(0, 0, size, size);
        drawable.draw(canvas);
        return bitmap;
    }

    private void showStatus(final String text, final boolean endExit) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                status.setText(text);
                if (endExit) {
                    finish();
                }
            }
        });
    }

    private static final class CommandOutcome {
        final int exitCode;
        final String output;

        CommandOutcome(int exitCode, String output) {
            this.exitCode = exitCode;
            this.output = output == null ? "" : output;
        }
    }
}
