package com.communism420.acbridge;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.widget.TextView;

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
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public final class MainActivity extends Activity {
    private static final String LABEL_SEPARATOR = "\\+\\";
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        status = new TextView(this);
        status.setText("OpenADB Bridge is exporting app data...");
        status.setPadding(24, 24, 24, 24);
        setContentView(status);

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
        File dataTemp = new File(outputDir, ".acbridge.tmp");
        File metadataTemp = new File(outputDir, "metadata.tsv.tmp");
        File iconsTemp = new File(outputDir, "icons.zip.tmp");
        dataFile.delete();
        metadataFile.delete();
        iconsFile.delete();
        errorFile.delete();
        progressFile.delete();

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
            replace(dataTemp, dataFile);
            replace(metadataTemp, metadataFile);
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

    private File outputDir() {
        File publicDir = new File(Environment.getExternalStorageDirectory(), ".adac");
        if (ensureDirectory(publicDir)) {
            return publicDir;
        }
        File appDir = appOutputDir();
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
}
