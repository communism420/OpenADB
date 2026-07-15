package com.communism420.acbridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.UriPermission;
import android.database.Cursor;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;
import android.webkit.MimeTypeMap;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * One-shot LAN receiver controlled by ADB and scoped by an existing SAF grant.
 *
 * OpenADB forwards a request-specific abstract LocalSocket through ADB. Only
 * Android's shell/root UID may connect, and the secret-bearing bootstrap is
 * streamed over that socket instead of being placed in an argv or filesystem
 * hand-off. A separate secret authenticates the READY metadata, and a fresh
 * token authenticates the complete request transcript and terminal response
 * of the only accepted TCP connection. The server stops after that connection,
 * an authenticated control-channel cancellation, or a bounded timeout.
 */
public final class P2PTransferService extends Service {
    private static final byte[] MAGIC = "OADBP2P2".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] REQUEST_TRANSCRIPT_CONTEXT =
            "OpenADB-P2P-request-v2\0".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] ENTRY_CONTROL_CONTEXT =
            "OpenADB-P2P-entry-v2\0".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] RESPONSE_CONTEXT =
            "OpenADB-P2P-response-v2\0".getBytes(StandardCharsets.US_ASCII);
    private static final String CHANNEL_ID = "openadb_p2p_transfer";
    private static final int NOTIFICATION_ID = 42044;
    private static final int MAX_ENTRIES = 100000;
    private static final int MAX_TEXT_BYTES = 65536;
    private static final int DEFAULT_TIMEOUT_SECONDS = 120;
    private static final int STORAGE_PERMISSION_TIMEOUT_SECONDS = 660;
    private static final int CONTROL_ACCEPT_TIMEOUT_SECONDS = 45;
    private static final int CONTROL_BOOTSTRAP_READ_TIMEOUT_MILLIS = 5000;
    private static final int ROOT_UID = 0;
    private static final int ADB_SHELL_UID = 2000;
    private static final int COPY_BUFFER_SIZE = 1024 * 1024;
    private static final String PREFS = "openadb_bridge";
    private static final String PREF_LAST_TREE_URI = "last_tree_uri";
    private static final String PREF_TREE_URI_PREFIX = "tree_uri_";
    private static final String PREF_DIRECT_VOLUME_PREFIX = "direct_volume_";
    private static final String CONTROL_SOCKET_PREFIX = "openadb_p2p_control_";

    private final Set<ServerSocket> activeServers = Collections.newSetFromMap(
            new ConcurrentHashMap<ServerSocket, Boolean>()
    );
    private final Set<Socket> activeSockets = Collections.newSetFromMap(
            new ConcurrentHashMap<Socket, Boolean>()
    );
    private final ConcurrentHashMap<String, ControlSession> activeControlSessions =
            new ConcurrentHashMap<String, ControlSession>();
    private final AtomicInteger activeSessionCount = new AtomicInteger();
    private final AtomicInteger latestStartId = new AtomicInteger();
    private final Object sessionLifecycleLock = new Object();
    private final Object directoryMutationLock = new Object();
    private volatile boolean stopping;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIFICATION_ID, notification("Waiting for OpenADB P2P transfer"));
        String requestId = intent == null ? "" : safeRequestId(intent.getStringExtra("request_id"));
        String cancelId = intent == null ? "" : safeRequestId(intent.getStringExtra("cancel_id"));
        synchronized (sessionLifecycleLock) {
            latestStartId.set(startId);
        }
        if (cancelId.length() > 0) {
            ControlSession target = activeControlSessions.get(cancelId);
            if (target != null) {
                target.cancel();
            }
            stopIfIdle(startId);
            return START_NOT_STICKY;
        }
        if (requestId.length() == 0) {
            stopIfIdle(startId);
            return START_NOT_STICKY;
        }
        final ControlSession controlSession = new ControlSession(requestId);
        if (activeControlSessions.putIfAbsent(requestId, controlSession) != null) {
            return START_NOT_STICKY;
        }
        synchronized (sessionLifecycleLock) {
            activeSessionCount.incrementAndGet();
        }
        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    runSession(controlSession);
                } finally {
                    controlSession.finish();
                    activeControlSessions.remove(controlSession.requestId, controlSession);
                    synchronized (sessionLifecycleLock) {
                        if (activeSessionCount.decrementAndGet() == 0) {
                            if (stopSelfResult(latestStartId.get())) {
                                stopForeground(true);
                            }
                        }
                    }
                }
            }
        }, "OpenADB-P2P-" + controlSession.requestId.substring(0, 8));
        worker.start();
        return START_NOT_STICKY;
    }

    private void stopIfIdle(int startId) {
        boolean stopImmediately;
        synchronized (sessionLifecycleLock) {
            stopImmediately = activeSessionCount.get() == 0;
        }
        if (stopImmediately && stopSelfResult(startId)) {
            stopForeground(true);
        }
    }

    @Override
    public void onDestroy() {
        stopping = true;
        for (ControlSession session : activeControlSessions.values()) {
            session.cancel();
        }
        for (Socket socket : activeSockets) {
            closeQuietly(socket);
        }
        for (ServerSocket server : activeServers) {
            closeQuietly(server);
        }
        activeSockets.clear();
        activeServers.clear();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void runSession(ControlSession controlSession) {
        Thread controlMonitor = null;
        ServerSocket server = null;
        Socket socket = null;
        try {
            LocalSocket controlSocket = acceptControlSocket(controlSession);
            controlSession.attachControlSocket(controlSocket);
            controlSocket.setSoTimeout(CONTROL_BOOTSTRAP_READ_TIMEOUT_MILLIS);
            InputStream controlInput = controlSocket.getInputStream();
            controlSession.attachControlOutput(controlSocket.getOutputStream());

            SessionRequest request = readControlRequest(controlInput);
            throwIfCancelled(controlSession);
            // ADB may accept the host-side forwarded TCP socket before adbd
            // has reached this abstract endpoint. Confirm that ACBridge itself
            // parsed the complete secret-bearing request before the desktop
            // treats the control channel as established.
            writeControlLine(controlSession, "ACCEPTED");
            // Android LocalSocket reports an idle SO_TIMEOUT as a plain
            // IOException on some releases. The monitor must remain blocking
            // while SAF permission or the LAN peer is pending; cancellation
            // closes the request-specific socket and unblocks this read.
            controlSocket.setSoTimeout(0);
            controlMonitor = startControlMonitor(controlSession, controlInput);

            // Do not open a socket or accept file bytes until Android has
            // granted ACBridge access to the requested storage tree.
            DestinationAccess destinationAccess = waitForDestinationAccess(
                    request.destination,
                    controlSession
            );
            throwIfCancelled(controlSession);
            String token = randomHex(32);
            server = new ServerSocket();
            controlSession.attachDataServer(server);
            activeServers.add(server);
            server.setReuseAddress(true);
            server.bind(new InetSocketAddress(request.port), 1);
            server.setSoTimeout(250);
            String ready = "READY\t" + server.getLocalPort() + "\t" + token + "\t"
                    + (System.currentTimeMillis() + request.timeoutSeconds * 1000L);
            String bootstrapProof = hexString(
                    hmac(hexBytes(request.bootstrapSecret), ready.getBytes(StandardCharsets.UTF_8))
            );
            controlSession.readyPublished = true;
            try {
                writeControlLine(controlSession, ready + "\t" + bootstrapProof);
            } catch (Exception exc) {
                controlSession.readyPublished = false;
                throw exc;
            }
            long acceptDeadline = System.currentTimeMillis() + request.timeoutSeconds * 1000L;
            while (socket == null && System.currentTimeMillis() < acceptDeadline) {
                throwIfCancelled(controlSession);
                try {
                    socket = server.accept();
                } catch (SocketTimeoutException waiting) {
                    // Poll the request-specific control state so Cancel remains prompt.
                }
            }
            if (socket == null) {
                throw new SocketTimeoutException("P2P client did not connect before timeout");
            }
            controlSession.attachDataSocket(socket);
            activeSockets.add(socket);
            socket.setSoTimeout(request.timeoutSeconds * 1000);
            throwIfCancelled(controlSession);
            updateNotification("Receiving files from OpenADB");
            handleUpload(socket, token, destinationAccess);
        } catch (Exception exc) {
            if (!controlSession.isCancelled()) {
                writeControlErrorSafely(controlSession, cleanMessage(exc));
            }
        } finally {
            controlSession.completed = true;
            closeQuietly(socket);
            closeQuietly(server);
            if (socket != null) {
                activeSockets.remove(socket);
            }
            if (server != null) {
                activeServers.remove(server);
            }
            controlSession.closeControlResources();
            joinControlMonitor(controlMonitor);
        }
    }

    private DestinationAccess waitForDestinationAccess(
            String destination,
            ControlSession controlSession
    ) throws Exception {
        long deadline = System.currentTimeMillis() + STORAGE_PERMISSION_TIMEOUT_SECONDS * 1000L;
        boolean permissionPublished = false;
        while (!stopping && System.currentTimeMillis() < deadline) {
            throwIfCancelled(controlSession);
            try {
                return resolveDestinationAccess(destination);
            } catch (SecurityException permissionMissing) {
                if (!permissionPublished) {
                    writeControlLine(controlSession, "PERMISSION_REQUIRED\t" + destination);
                    updateNotification("Grant MicroSD/USB access on the Android device");
                    permissionPublished = true;
                }
                Thread.sleep(250L);
            }
        }
        if (stopping) {
            throw new InterruptedException("P2P storage permission request was cancelled");
        }
        throw new SecurityException(
                "SAF_PERMISSION_TIMEOUT: storage access was not granted before the P2P session timeout: "
                        + destination
        );
    }

    private DestinationAccess resolveDestinationAccess(String destination) throws Exception {
        String clean = normalizeStoragePath(destination);

        // MANAGE_EXTERNAL_STORAGE is reliable for primary shared storage, but
        // several Android TV firmwares expose removable volumes as readable
        // while still rejecting FileOutputStream with EACCES. Prefer a
        // persisted SAF tree for every MicroSD/USB destination, even when the
        // global All files access switch is enabled.
        if (isInternalSharedStoragePath(clean)) {
            if (hasAllFilesAccess()) {
                File directDirectory = resolveDirectDestinationDirectory(clean);
                verifyDirectDestinationWritable(directDirectory);
                return DestinationAccess.direct(directDirectory);
            }
            return DestinationAccess.saf(resolveDestinationDirectory(clean));
        }

        SecurityException missingSaf;
        try {
            return DestinationAccess.saf(resolveDestinationDirectory(clean));
        } catch (SecurityException exc) {
            missingSaf = exc;
        }

        // Keep compatibility with TV firmware that has no usable system tree
        // picker. A global app-op alone is insufficient: MainActivity must
        // first record that the permission flow approved this exact volume.
        // The fallback is then accepted only after an actual create/delete
        // probe succeeds before READY is published.
        if (hasApprovedDirectFallback(clean) && hasAllFilesAccess()) {
            try {
                File directDirectory = resolveDirectDestinationDirectory(clean);
                verifyDirectDestinationWritable(directDirectory);
                return DestinationAccess.direct(directDirectory);
            } catch (Exception directFailure) {
                clearDirectFallbackApproval(clean);
                missingSaf.addSuppressed(directFailure);
            }
        }
        throw missingSaf;
    }

    private void verifyDirectDestinationWritable(File directory) throws Exception {
        File probe = new File(directory, ".openadb-" + randomHex(8) + ".probe");
        OutputStream output = null;
        boolean deleted = false;
        try {
            output = new FileOutputStream(probe);
            output.close();
            output = null;
            deleted = probe.delete();
            if (!deleted) {
                throw new java.io.IOException(
                        "Direct storage access could not remove its write probe"
                );
            }
        } finally {
            closeQuietly(output);
            if (!deleted && probe.exists()) {
                probe.delete();
            }
        }
    }

    private boolean hasApprovedDirectFallback(String destination) {
        String key = storagePreferenceKey(PREF_DIRECT_VOLUME_PREFIX, destination);
        if (key.length() == 0) {
            return false;
        }
        try {
            return getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean(key, false);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void clearDirectFallbackApproval(String destination) {
        String key = storagePreferenceKey(PREF_DIRECT_VOLUME_PREFIX, destination);
        if (key.length() == 0) {
            return;
        }
        try {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(key).apply();
        } catch (Throwable ignored) {
        }
    }

    private void throwIfCancelled(ControlSession controlSession) throws InterruptedException {
        if (stopping || controlSession.isCancelled()) {
            throw new InterruptedException("P2P transfer was cancelled");
        }
    }

    private LocalSocket acceptControlSocket(final ControlSession controlSession) throws Exception {
        throwIfCancelled(controlSession);
        final LocalServerSocket listener = new LocalServerSocket(
                CONTROL_SOCKET_PREFIX + controlSession.requestId
        );
        controlSession.attachControlListener(listener);
        throwIfCancelled(controlSession);

        final AtomicBoolean acceptFinished = new AtomicBoolean(false);
        final long acceptDeadline = System.currentTimeMillis()
                + CONTROL_ACCEPT_TIMEOUT_SECONDS * 1000L;
        Thread acceptWatchdog = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    while (!acceptFinished.get() && !controlSession.isCancelled()) {
                        long remaining = acceptDeadline - System.currentTimeMillis();
                        if (remaining <= 0L) {
                            controlSession.controlAcceptExpired = true;
                            closeQuietly(listener);
                            return;
                        }
                        Thread.sleep(Math.min(250L, remaining));
                    }
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
            }
        }, "OpenADB-P2P-control-timeout-" + controlSession.requestId.substring(0, 8));
        acceptWatchdog.start();

        try {
            while (true) {
                throwIfCancelled(controlSession);
                LocalSocket candidate;
                try {
                    candidate = listener.accept();
                } catch (IOException exc) {
                    if (controlSession.controlAcceptExpired) {
                        throw new SocketTimeoutException(
                                "ADB did not open the one-time P2P control channel before timeout"
                        );
                    }
                    throwIfCancelled(controlSession);
                    throw exc;
                }
                Credentials credentials = null;
                try {
                    credentials = candidate.getPeerCredentials();
                } catch (IOException ignored) {
                    // Missing credentials never downgrade the shell/root-only policy.
                }
                int uid = credentials == null ? -1 : credentials.getUid();
                if (uid == ADB_SHELL_UID || uid == ROOT_UID) {
                    return candidate;
                }
                try {
                    writeRawControlLine(
                            candidate.getOutputStream(),
                            "ERROR\tP2P control access is restricted to the ADB shell"
                    );
                } catch (Throwable ignored) {
                } finally {
                    closeQuietly(candidate);
                }
            }
        } finally {
            acceptFinished.set(true);
            acceptWatchdog.interrupt();
            closeQuietly(listener);
            joinThreadQuietly(acceptWatchdog, 1000L);
        }
    }

    private SessionRequest readControlRequest(InputStream input) throws Exception {
        String version = readControlLine(input, 64);
        String portText = readControlLine(input, 16);
        String timeoutText = readControlLine(input, 16);
        String destination = readControlLine(input, MAX_TEXT_BYTES);
        String bootstrapSecret = readControlLine(input, 128);
        if (!"OPENADB_P2P_2".equals(version)
                || portText == null
                || timeoutText == null
                || destination == null
                || bootstrapSecret == null
                || bootstrapSecret.length() != 64) {
            throw new IOException("invalid control request format");
        }
        hexBytes(bootstrapSecret);
        int port = Integer.parseInt(portText);
        int timeout = Integer.parseInt(timeoutText);
        if (port != 0 && (port < 1024 || port > 65535)) {
            throw new IllegalArgumentException("invalid TCP port");
        }
        timeout = Math.max(30, Math.min(600, timeout));
        destination = normalizeStoragePath(destination);
        if (!destination.startsWith("/storage/") || destination.indexOf('\0') >= 0) {
            throw new IllegalArgumentException(
                    "destination is not an Android shared storage path"
            );
        }
        return new SessionRequest(port, timeout, destination, bootstrapSecret);
    }

    private Thread startControlMonitor(
            final ControlSession controlSession,
            final InputStream input
    ) {
        Thread monitor = new Thread(new Runnable() {
            @Override
            public void run() {
                while (!controlSession.completed && !controlSession.isCancelled()) {
                    String command;
                    try {
                        command = readControlLine(input, 64);
                    } catch (Throwable exc) {
                        if (!controlSession.completed && !controlSession.controlCloseRequested) {
                            handleControlDisconnect(controlSession);
                        }
                        return;
                    }
                    if (command == null) {
                        if (!controlSession.completed && !controlSession.controlCloseRequested) {
                            handleControlDisconnect(controlSession);
                        }
                        return;
                    }
                    if ("CANCEL".equals(command)) {
                        controlSession.cancel();
                        return;
                    }
                    if ("CLOSE".equals(command)) {
                        if (controlSession.readyPublished) {
                            controlSession.controlCloseRequested = true;
                        } else {
                            controlSession.cancel();
                        }
                        return;
                    }
                    writeControlErrorSafely(
                            controlSession,
                            "Unsupported P2P control command"
                    );
                    controlSession.cancel();
                    return;
                }
            }
        }, "OpenADB-P2P-control-" + controlSession.requestId.substring(0, 8));
        controlSession.controlMonitor = monitor;
        monitor.start();
        return monitor;
    }

    private void handleControlDisconnect(ControlSession controlSession) {
        if (controlSession.readyPublished) {
            // The authenticated LAN transfer can finish if Wireless ADB drops
            // after READY. Its own socket timeout still bounds the orphan.
            controlSession.controlCloseRequested = true;
        } else {
            controlSession.cancel();
        }
    }

    private String readControlLine(InputStream input, int maxBytes) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        while (true) {
            int value = input.read();
            if (value < 0) {
                if (buffer.size() == 0) {
                    return null;
                }
                break;
            }
            if (value == '\n') {
                break;
            }
            if (buffer.size() >= maxBytes) {
                throw new IOException("oversized P2P control line");
            }
            buffer.write(value);
        }
        byte[] bytes = buffer.toByteArray();
        int length = bytes.length;
        if (length > 0 && bytes[length - 1] == '\r') {
            length--;
        }
        return new String(bytes, 0, length, StandardCharsets.UTF_8);
    }

    private void writeControlLine(ControlSession controlSession, String text) throws IOException {
        OutputStream output = controlSession.controlOutput;
        if (output == null) {
            throw new IOException("P2P control channel is unavailable");
        }
        synchronized (controlSession.outputLock) {
            writeRawControlLine(output, text);
        }
    }

    private void writeRawControlLine(OutputStream output, String text) throws IOException {
        String safe = text == null ? "" : text.replace('\n', ' ').replace('\r', ' ');
        byte[] payload = (safe + "\n").getBytes(StandardCharsets.UTF_8);
        if (payload.length > MAX_TEXT_BYTES + 256) {
            throw new IOException("oversized P2P control response");
        }
        output.write(payload);
        output.flush();
    }

    private void writeControlErrorSafely(ControlSession controlSession, String message) {
        if (!controlSession.errorPublished.compareAndSet(false, true)) {
            return;
        }
        try {
            writeControlLine(controlSession, "ERROR\t" + cleanMessageText(message));
        } catch (Throwable ignored) {
        }
    }

    private void joinControlMonitor(Thread monitor) {
        if (monitor == null || monitor == Thread.currentThread()) {
            return;
        }
        monitor.interrupt();
        joinThreadQuietly(monitor, 1000L);
    }

    private void joinThreadQuietly(Thread thread, long timeoutMillis) {
        try {
            thread.join(timeoutMillis);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private void handleUpload(
            Socket socket,
            String token,
            DestinationAccess destinationAccess
    ) throws Exception {
        DataInputStream input = new DataInputStream(new BufferedInputStream(socket.getInputStream(), COPY_BUFFER_SIZE));
        DataOutputStream output = new DataOutputStream(new BufferedOutputStream(socket.getOutputStream(), 65536));
        byte[] magic = new byte[MAGIC.length];
        input.readFully(magic);
        if (!MessageDigest.isEqual(MAGIC, magic)) {
            throw new SecurityException("Unsupported P2P protocol");
        }
        byte[] sessionKey = hexBytes(token);
        byte[] receivedProof = new byte[32];
        input.readFully(receivedProof);
        if (!MessageDigest.isEqual(hmac(sessionKey, MAGIC), receivedProof)) {
            throw new SecurityException("P2P authentication failed");
        }

        Mac requestTranscript = newHmac(sessionKey);
        requestTranscript.update(REQUEST_TRANSCRIPT_CONTEXT);
        int entryCount = input.readInt();
        requestTranscript.update(intBytes(entryCount));
        if (entryCount < 1 || entryCount > MAX_ENTRIES) {
            throw new IllegalArgumentException("Invalid transfer entry count: " + entryCount);
        }

        boolean directAccess = destinationAccess.directDirectory != null;
        SafDirectory destinationDirectory = destinationAccess.safDirectory;
        File directDestination = destinationAccess.directDirectory;
        int receivedEntries = 0;
        int receivedFiles = 0;
        long receivedBytes = 0L;
        for (int index = 0; index < entryCount; index++) {
            int kind = input.readUnsignedByte();
            requestTranscript.update((byte) kind);
            String relativePath = validateRelativePath(readAuthenticatedText(input, requestTranscript));
            long size = -1L;
            if (kind == 1) {
                size = input.readLong();
                requestTranscript.update(longBytes(size));
                if (size < 0) {
                    throw new IllegalArgumentException("Negative file size for " + relativePath);
                }
            } else if (kind != 0) {
                throw new IllegalArgumentException("Unsupported transfer entry type: " + kind);
            }

            byte[] controlTag = new byte[32];
            input.readFully(controlTag);
            requestTranscript.update(controlTag);
            byte[] controlFrame = entryControlFrame(index, kind, relativePath, size);
            if (!MessageDigest.isEqual(hmac(sessionKey, controlFrame), controlTag)) {
                throw new SecurityException("P2P entry metadata authentication failed");
            }

            if (kind == 0) {
                if (directAccess) {
                    ensureDirectDirectory(directDestination, relativePath);
                } else {
                    ensureRelativeDirectory(destinationDirectory, relativePath);
                }
            } else {
                if (directAccess) {
                    receiveDirectFile(
                            directDestination,
                            relativePath,
                            size,
                            sessionKey,
                            input,
                            requestTranscript
                    );
                } else {
                    receiveFile(
                            destinationDirectory,
                            relativePath,
                            size,
                            sessionKey,
                            input,
                            requestTranscript
                    );
                }
                receivedFiles++;
                if (Long.MAX_VALUE - receivedBytes < size) {
                    throw new IllegalArgumentException("Transfer byte count overflow");
                }
                receivedBytes += size;
            }
            receivedEntries++;
        }

        byte[] suppliedRequestTag = new byte[32];
        input.readFully(suppliedRequestTag);
        byte[] verifiedRequestTag = requestTranscript.doFinal();
        if (!MessageDigest.isEqual(verifiedRequestTag, suppliedRequestTag)) {
            throw new SecurityException("P2P request transcript authentication failed");
        }
        writeAuthenticatedResponse(
                output,
                sessionKey,
                verifiedRequestTag,
                true,
                receivedEntries,
                receivedFiles,
                receivedBytes,
                "Stored " + entryCount + " item(s) through ACBridge "
                        + (directAccess ? "All files access" : "SAF access")
        );
    }

    private void receiveFile(
            SafDirectory base,
            String relativePath,
            long size,
            byte[] sessionKey,
            DataInputStream input,
            Mac requestTranscript
    ) throws Exception {
        List<String> parts = pathComponents(relativePath);
        String fileName = parts.remove(parts.size() - 1);
        SafDirectory parent = ensureDirectoryComponents(base, parts);
        ChildDocument existingChild = findChild(parent, fileName);
        if (existingChild != null && Document.MIME_TYPE_DIR.equals(existingChild.mimeType)) {
            throw new java.io.IOException("A directory already exists where a file would be written: " + relativePath);
        }
        Uri existing = existingChild == null ? null : existingChild.uri;
        String tempName = ".openadb-" + randomHex(8) + ".part";
        Uri temp = DocumentsContract.createDocument(
                getContentResolver(),
                parent.documentUri,
                mimeType(fileName),
                tempName
        );
        if (temp == null) {
            throw new java.io.IOException("SAF could not create a temporary file for " + relativePath);
        }

        OutputStream rawOutput = null;
        boolean committed = false;
        try {
            rawOutput = getContentResolver().openOutputStream(temp, "w");
            if (rawOutput == null) {
                throw new java.io.IOException("SAF could not open " + relativePath + " for writing");
            }
            receiveVerifiedPayload(
                    relativePath,
                    size,
                    sessionKey,
                    input,
                    rawOutput,
                    requestTranscript
            );
            rawOutput.close();
            rawOutput = null;
            if (existing != null && !DocumentsContract.deleteDocument(getContentResolver(), existing)) {
                throw new java.io.IOException("Could not replace existing file " + relativePath);
            }
            Uri renamed = null;
            try {
                renamed = DocumentsContract.renameDocument(getContentResolver(), temp, fileName);
            } catch (Throwable ignored) {
            }
            if (renamed == null) {
                copyDocument(temp, parent, fileName);
                DocumentsContract.deleteDocument(getContentResolver(), temp);
            }
            committed = true;
        } finally {
            if (rawOutput != null) {
                try {
                    rawOutput.close();
                } catch (Throwable ignored) {
                }
            }
            if (!committed) {
                try {
                    DocumentsContract.deleteDocument(getContentResolver(), temp);
                } catch (Throwable ignored) {
                }
            }
        }
    }

    private void receiveDirectFile(
            File base,
            String relativePath,
            long size,
            byte[] sessionKey,
            DataInputStream input,
            Mac requestTranscript
    ) throws Exception {
        File target = resolveDirectChild(base, relativePath);
        File parent = target.getParentFile();
        synchronized (directoryMutationLock) {
            if (parent == null || (!parent.isDirectory() && !parent.mkdirs() && !parent.isDirectory())) {
                throw new java.io.IOException("Could not create destination directory for " + relativePath);
            }
        }
        if (target.isDirectory()) {
            throw new java.io.IOException("A directory already exists where a file would be written: " + relativePath);
        }
        File temp = new File(parent, ".openadb-" + randomHex(8) + ".part");
        OutputStream output = null;
        boolean committed = false;
        try {
            output = new FileOutputStream(temp);
            receiveVerifiedPayload(
                    relativePath,
                    size,
                    sessionKey,
                    input,
                    output,
                    requestTranscript
            );
            output.close();
            output = null;
            if (target.exists() && !target.delete()) {
                throw new java.io.IOException("Could not replace existing file " + relativePath);
            }
            if (!temp.renameTo(target)) {
                copyDirectFile(temp, target);
                if (!temp.delete()) {
                    temp.deleteOnExit();
                }
            }
            committed = true;
        } finally {
            closeQuietly(output);
            if (!committed && temp.exists()) {
                temp.delete();
            }
        }
    }

    private void receiveVerifiedPayload(
            String relativePath,
            long size,
            byte[] sessionKey,
            DataInputStream input,
            OutputStream destination,
            Mac requestTranscript
    ) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        Mac authenticator = Mac.getInstance("HmacSHA256");
        authenticator.init(new SecretKeySpec(sessionKey, "HmacSHA256"));
        authenticator.update(relativePath.getBytes(StandardCharsets.UTF_8));
        authenticator.update((byte) 0);
        authenticator.update(ByteBuffer.allocate(8).putLong(size).array());
        BufferedOutputStream output = new BufferedOutputStream(destination, COPY_BUFFER_SIZE);
        byte[] buffer = new byte[COPY_BUFFER_SIZE];
        long remaining = size;
        while (remaining > 0) {
            int wanted = (int) Math.min((long) buffer.length, remaining);
            int read = input.read(buffer, 0, wanted);
            if (read < 0) {
                throw new java.io.IOException("Connection ended before " + relativePath + " was complete");
            }
            output.write(buffer, 0, read);
            digest.update(buffer, 0, read);
            authenticator.update(buffer, 0, read);
            remaining -= read;
        }
        output.flush();
        byte[] expectedDigest = new byte[32];
        input.readFully(expectedDigest);
        requestTranscript.update(expectedDigest);
        if (!MessageDigest.isEqual(expectedDigest, digest.digest())) {
            throw new java.io.IOException("SHA-256 verification failed for " + relativePath);
        }
        byte[] expectedAuthenticator = new byte[32];
        input.readFully(expectedAuthenticator);
        requestTranscript.update(expectedAuthenticator);
        if (!MessageDigest.isEqual(expectedAuthenticator, authenticator.doFinal())) {
            throw new SecurityException("P2P integrity verification failed for " + relativePath);
        }
    }

    private boolean hasAllFilesAccess() {
        return Build.VERSION.SDK_INT >= 30 && Environment.isExternalStorageManager();
    }

    private File resolveDirectDestinationDirectory(String destination) throws Exception {
        String clean = normalizeStoragePath(destination);
        String storageId = storageIdFromPath(clean);
        if (storageId.length() == 0) {
            throw new SecurityException("Destination is not a public Android storage path: " + clean);
        }
        File storageRoot = isInternalSharedStoragePath(clean)
                ? new File("/storage/emulated/0").getCanonicalFile()
                : new File("/storage", storageId).getCanonicalFile();
        File target = new File(clean).getCanonicalFile();
        String rootPath = storageRoot.getPath();
        String targetPath = target.getPath();
        if (!targetPath.equals(rootPath) && !targetPath.startsWith(rootPath + File.separator)) {
            throw new SecurityException("Destination escapes the selected storage volume: " + clean);
        }
        synchronized (directoryMutationLock) {
            if (!target.isDirectory() && !target.mkdirs() && !target.isDirectory()) {
                throw new java.io.IOException("Could not create or open P2P destination: " + clean);
            }
        }
        return target;
    }

    private File ensureDirectDirectory(File base, String relativePath) throws Exception {
        File target = resolveDirectChild(base, relativePath);
        synchronized (directoryMutationLock) {
            if (target.isFile()) {
                throw new java.io.IOException("A file blocks destination directory " + relativePath);
            }
            if (!target.isDirectory() && !target.mkdirs() && !target.isDirectory()) {
                throw new java.io.IOException("Could not create destination directory " + relativePath);
            }
        }
        return target;
    }

    private File resolveDirectChild(File base, String relativePath) throws Exception {
        File canonicalBase = base.getCanonicalFile();
        File target = new File(canonicalBase, relativePath).getCanonicalFile();
        String basePath = canonicalBase.getPath();
        String targetPath = target.getPath();
        if (!targetPath.startsWith(basePath + File.separator)) {
            throw new SecurityException("Unsafe P2P destination path: " + relativePath);
        }
        return target;
    }

    private void copyDirectFile(File source, File target) throws Exception {
        InputStream input = null;
        OutputStream output = null;
        try {
            input = new FileInputStream(source);
            output = new FileOutputStream(target);
            byte[] buffer = new byte[COPY_BUFFER_SIZE];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) {
                    output.write(buffer, 0, read);
                }
            }
            output.flush();
        } catch (Exception exc) {
            target.delete();
            throw exc;
        } finally {
            closeQuietly(input);
            closeQuietly(output);
        }
    }

    private void copyDocument(Uri source, SafDirectory parent, String fileName) throws Exception {
        Uri target = DocumentsContract.createDocument(getContentResolver(), parent.documentUri, mimeType(fileName), fileName);
        if (target == null) {
            throw new java.io.IOException("SAF provider cannot rename or create " + fileName);
        }
        InputStream input = null;
        OutputStream output = null;
        try {
            input = getContentResolver().openInputStream(source);
            output = getContentResolver().openOutputStream(target, "w");
            if (input == null || output == null) {
                throw new java.io.IOException("SAF provider cannot finalize " + fileName);
            }
            byte[] buffer = new byte[COPY_BUFFER_SIZE];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) {
                    output.write(buffer, 0, read);
                }
            }
            output.flush();
        } catch (Exception exc) {
            try {
                DocumentsContract.deleteDocument(getContentResolver(), target);
            } catch (Throwable ignored) {
            }
            throw exc;
        } finally {
            closeQuietly(input);
            closeQuietly(output);
        }
    }

    private SafDirectory resolveDestinationDirectory(String destination) throws Exception {
        String clean = normalizeStoragePath(destination);
        String storageId = storageIdFromPath(clean);
        for (Uri treeUri : persistedTreeUris(clean)) {
            String treeId;
            try {
                treeId = DocumentsContract.getTreeDocumentId(treeUri);
            } catch (Throwable ignored) {
                continue;
            }
            String treeVolume = volumeFromDocumentId(treeId);
            if (!storageMatchesTree(storageId, treeVolume)) {
                continue;
            }
            String relative = relativePathFromStoragePath(clean);
            String treeRelative = relativeFromDocumentId(treeId);
            if (treeRelative.length() > 0) {
                if (relative.equals(treeRelative)) {
                    relative = "";
                } else {
                    String prefix = treeRelative + "/";
                    if (!relative.startsWith(prefix)) {
                        continue;
                    }
                    relative = relative.substring(prefix.length());
                }
            }
            SafDirectory resolved;
            try {
                Uri rootUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, treeId);
                SafDirectory root = new SafDirectory(treeUri, treeId, rootUri);
                resolved = ensureDirectoryComponents(root, pathComponents(relative));
            } catch (SecurityException ignored) {
                // A stale persisted tree should lead to a fresh SAF grant.
                discardStaleTreeGrant(treeUri);
                continue;
            } catch (FileNotFoundException ignored) {
                // An unmounted or replaced provider tree is stale as well.
                discardStaleTreeGrant(treeUri);
                continue;
            }
            try {
                verifySafDestinationWritable(resolved);
            } catch (SecurityException exc) {
                discardStaleTreeGrant(treeUri);
                continue;
            } catch (FileNotFoundException exc) {
                throw new IOException(
                        "SAF destination became unavailable while checking write access.",
                        exc
                );
            }
            return resolved;
        }
        throw new SecurityException(
                "SAF_PERMISSION_REQUIRED: grant ACBridge access to this MicroSD/USB location before using P2P: " + clean
        );
    }

    private void verifySafDestinationWritable(SafDirectory directory) throws Exception {
        synchronized (directoryMutationLock) {
            Uri probe = null;
            OutputStream output = null;
            boolean deleted = false;
            try {
                probe = DocumentsContract.createDocument(
                        getContentResolver(),
                        directory.documentUri,
                        "application/octet-stream",
                        ".openadb-" + randomHex(8) + ".probe"
                );
                if (probe == null) {
                    throw new java.io.IOException(
                            "SAF provider could not create a write probe in the destination"
                    );
                }
                output = getContentResolver().openOutputStream(probe, "w");
                if (output == null) {
                    throw new java.io.IOException(
                            "SAF provider could not open its destination write probe"
                    );
                }
                output.close();
                output = null;
                try {
                    deleted = DocumentsContract.deleteDocument(getContentResolver(), probe);
                } catch (SecurityException exc) {
                    throw new java.io.IOException(
                            "SAF provider could not remove its destination write probe",
                            exc
                    );
                }
                if (!deleted) {
                    throw new java.io.IOException(
                            "SAF provider could not remove its destination write probe"
                    );
                }
            } finally {
                closeQuietly(output);
                if (probe != null && !deleted) {
                    try {
                        DocumentsContract.deleteDocument(getContentResolver(), probe);
                    } catch (Throwable ignored) {
                    }
                }
            }
        }
    }

    private void discardStaleTreeGrant(Uri treeUri) throws IOException {
        Throwable releaseFailure = null;
        try {
            getContentResolver().releasePersistableUriPermission(
                    treeUri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            );
        } catch (Throwable exc) {
            releaseFailure = exc;
        }
        try {
            SharedPreferences preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
            SharedPreferences.Editor editor = preferences.edit();
            java.util.Map<String, ?> values = preferences.getAll();
            String rawLast = preferences.getString(PREF_LAST_TREE_URI, "");
            if (treeUri.toString().equals(rawLast)) {
                editor.remove(PREF_LAST_TREE_URI);
            }
            for (String key : values.keySet()) {
                Object value = values.get(key);
                if (key.startsWith(PREF_TREE_URI_PREFIX)
                        && value instanceof String
                        && treeUri.toString().equals(value)) {
                    editor.remove(key);
                }
            }
            editor.apply();
        } catch (Throwable exc) {
            throw new IOException(
                    "The persisted SAF location is unavailable and ACBridge could not clear its stale preference.",
                    exc
            );
        }
        if (hasActivePersistedTreeAccess(treeUri)) {
            Throwable cause = releaseFailure != null
                    ? releaseFailure
                    : new SecurityException("Android retained the stale SAF permission");
            throw new IOException(
                    "The persisted SAF location is unavailable and ACBridge could not reset its stale permission.",
                    cause
            );
        }
    }

    private boolean hasActivePersistedTreeAccess(Uri expectedUri) {
        if (expectedUri == null) {
            return false;
        }
        try {
            for (UriPermission permission : getContentResolver().getPersistedUriPermissions()) {
                if (permission != null
                        && permission.isReadPermission()
                        && permission.isWritePermission()
                        && permission.getUri() != null
                        && expectedUri.toString().equals(permission.getUri().toString())) {
                    return true;
                }
            }
        } catch (Throwable ignored) {
            return true;
        }
        return false;
    }

    private SafDirectory ensureRelativeDirectory(SafDirectory base, String relativePath) throws Exception {
        return ensureDirectoryComponents(base, pathComponents(relativePath));
    }

    private SafDirectory ensureDirectoryComponents(SafDirectory base, List<String> components) throws Exception {
        synchronized (directoryMutationLock) {
            SafDirectory current = base;
            for (String component : components) {
                ChildDocument child = findChild(current, component);
                if (child != null) {
                    if (!Document.MIME_TYPE_DIR.equals(child.mimeType)) {
                        throw new java.io.IOException("A file blocks destination directory " + component);
                    }
                    current = new SafDirectory(current.treeUri, child.documentId, child.uri);
                    continue;
                }
                Uri created = DocumentsContract.createDocument(
                        getContentResolver(),
                        current.documentUri,
                        Document.MIME_TYPE_DIR,
                        component
                );
                if (created == null) {
                    throw new java.io.IOException("SAF could not create directory " + component);
                }
                String documentId = DocumentsContract.getDocumentId(created);
                current = new SafDirectory(current.treeUri, documentId, created);
            }
            return current;
        }
    }

    private ChildDocument findChild(SafDirectory parent, String name) throws Exception {
        Cursor cursor = null;
        try {
            Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(parent.treeUri, parent.documentId);
            cursor = getContentResolver().query(
                    children,
                    new String[] {Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE},
                    null,
                    null,
                    null
            );
            if (cursor != null) {
                while (cursor.moveToNext()) {
                    if (name.equals(cursor.getString(1))) {
                        String id = cursor.getString(0);
                        Uri uri = DocumentsContract.buildDocumentUriUsingTree(parent.treeUri, id);
                        return new ChildDocument(id, uri, cursor.getString(2));
                    }
                }
            }
        } finally {
            if (cursor != null) {
                cursor.close();
            }
        }
        return null;
    }

    private List<Uri> persistedTreeUris(String destination) {
        ArrayList<Uri> active = new ArrayList<Uri>();
        try {
            for (UriPermission permission : getContentResolver().getPersistedUriPermissions()) {
                if (permission != null
                        && permission.isReadPermission()
                        && permission.isWritePermission()
                        && permission.getUri() != null
                        && !containsUri(active, permission.getUri())) {
                    active.add(permission.getUri());
                }
            }
        } catch (Throwable ignored) {
        }

        ArrayList<Uri> result = new ArrayList<Uri>();
        try {
            SharedPreferences preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
            addActivePreferenceUri(
                    result,
                    active,
                    preferences.getString(
                            storagePreferenceKey(PREF_TREE_URI_PREFIX, destination),
                            ""
                    )
            );
            addActivePreferenceUri(
                    result,
                    active,
                    preferences.getString(PREF_LAST_TREE_URI, "")
            );
        } catch (Throwable ignored) {
        }
        for (Uri treeUri : active) {
            if (!containsUri(result, treeUri)) {
                result.add(treeUri);
            }
        }
        return result;
    }

    private void addActivePreferenceUri(
            List<Uri> result,
            List<Uri> active,
            String rawUri
    ) {
        if (rawUri == null || rawUri.length() == 0) {
            return;
        }
        Uri candidate = Uri.parse(rawUri);
        if (containsUri(active, candidate) && !containsUri(result, candidate)) {
            result.add(candidate);
        }
    }

    private boolean containsUri(List<Uri> uris, Uri candidate) {
        for (Uri uri : uris) {
            if (uri.toString().equals(candidate.toString())) {
                return true;
            }
        }
        return false;
    }

    private String validateRelativePath(String path) {
        if (path == null || path.length() == 0 || path.length() > MAX_TEXT_BYTES) {
            throw new IllegalArgumentException("Invalid empty or oversized relative path");
        }
        String clean = path.replace('\\', '/');
        if (clean.startsWith("/") || clean.endsWith("/") || clean.indexOf('\0') >= 0) {
            throw new IllegalArgumentException("Invalid relative path: " + path);
        }
        for (String part : clean.split("/")) {
            if (part.length() == 0 || ".".equals(part) || "..".equals(part)) {
                throw new IllegalArgumentException("Unsafe relative path: " + path);
            }
        }
        return clean;
    }

    private List<String> pathComponents(String path) {
        ArrayList<String> parts = new ArrayList<String>();
        if (path == null || path.length() == 0) {
            return parts;
        }
        for (String part : path.replace('\\', '/').split("/")) {
            if (part.length() > 0) {
                parts.add(part);
            }
        }
        return parts;
    }

    private String readAuthenticatedText(DataInputStream input, Mac transcript) throws Exception {
        int length = input.readInt();
        transcript.update(intBytes(length));
        if (length < 0 || length > MAX_TEXT_BYTES) {
            throw new IllegalArgumentException("Invalid protocol text length: " + length);
        }
        byte[] bytes = new byte[length];
        input.readFully(bytes);
        transcript.update(bytes);
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private void writeText(DataOutputStream output, String value) throws Exception {
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
        output.writeInt(bytes.length);
        output.write(bytes);
    }

    private byte[] entryControlFrame(
            int index,
            int kind,
            String relativePath,
            long size
    ) throws Exception {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(buffer);
        output.write(ENTRY_CONTROL_CONTEXT);
        output.writeInt(index);
        output.writeByte(kind);
        writeText(output, relativePath);
        if (kind == 1) {
            output.writeLong(size);
        }
        output.flush();
        return buffer.toByteArray();
    }

    private void writeAuthenticatedResponse(
            DataOutputStream output,
            byte[] sessionKey,
            byte[] requestTag,
            boolean success,
            int entryCount,
            int fileCount,
            long byteCount,
            String message
    ) throws Exception {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        DataOutputStream response = new DataOutputStream(buffer);
        response.write(MAGIC);
        response.writeByte(success ? 1 : 0);
        response.writeInt(entryCount);
        response.writeInt(fileCount);
        response.writeLong(byteCount);
        writeText(response, message);
        response.flush();
        byte[] payload = buffer.toByteArray();

        Mac responseAuthenticator = newHmac(sessionKey);
        responseAuthenticator.update(RESPONSE_CONTEXT);
        responseAuthenticator.update(requestTag);
        responseAuthenticator.update(payload);
        output.write(payload);
        output.write(responseAuthenticator.doFinal());
        output.flush();
    }

    private Mac newHmac(byte[] key) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        return mac;
    }

    private byte[] intBytes(int value) {
        return ByteBuffer.allocate(4).putInt(value).array();
    }

    private byte[] longBytes(long value) {
        return ByteBuffer.allocate(8).putLong(value).array();
    }

    private String mimeType(String fileName) {
        int dot = fileName.lastIndexOf('.');
        if (dot >= 0 && dot + 1 < fileName.length()) {
            String extension = fileName.substring(dot + 1).toLowerCase(Locale.US);
            String known = MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension);
            if (known != null && known.length() > 0) {
                return known;
            }
        }
        return "application/octet-stream";
    }

    private String storageIdFromPath(String path) {
        String clean = normalizeStoragePath(path);
        if (isInternalSharedStoragePath(clean)) {
            return "primary";
        }
        String prefix = "/storage/";
        if (!clean.startsWith(prefix)) {
            return "";
        }
        int start = prefix.length();
        int slash = clean.indexOf('/', start);
        return slash < 0 ? clean.substring(start) : clean.substring(start, slash);
    }

    private String relativePathFromStoragePath(String path) {
        String clean = normalizeStoragePath(path);
        if (isInternalSharedStoragePath(clean)) {
            String root = "/storage/emulated/0";
            return clean.length() <= root.length() ? "" : clean.substring(root.length() + 1);
        }
        String prefix = "/storage/";
        if (!clean.startsWith(prefix)) {
            return "";
        }
        int slash = clean.indexOf('/', prefix.length());
        return slash < 0 || slash + 1 >= clean.length() ? "" : clean.substring(slash + 1);
    }

    private String volumeFromDocumentId(String documentId) {
        int separator = documentId == null ? -1 : documentId.indexOf(':');
        return separator >= 0 ? documentId.substring(0, separator) : String.valueOf(documentId);
    }

    private String relativeFromDocumentId(String documentId) {
        int separator = documentId == null ? -1 : documentId.indexOf(':');
        return separator < 0 || separator + 1 >= documentId.length() ? "" : documentId.substring(separator + 1);
    }

    private String storagePreferenceKey(String prefix, String path) {
        String storageId = storageIdFromPath(normalizeStoragePath(path));
        if (storageId.length() == 0) {
            return "";
        }
        return prefix + storageId.toLowerCase(Locale.US);
    }

    private boolean storageMatchesTree(String storageId, String treeVolume) {
        if (storageId == null || treeVolume == null || storageId.length() == 0 || treeVolume.length() == 0) {
            return false;
        }
        String left = storageId.replaceAll("[^0-9A-Fa-f]", "").toLowerCase(Locale.US);
        String right = treeVolume.replaceAll("[^0-9A-Fa-f]", "").toLowerCase(Locale.US);
        if (storageId.equalsIgnoreCase(treeVolume)) {
            return true;
        }
        if ("primary".equalsIgnoreCase(storageId) && "primary".equalsIgnoreCase(treeVolume)) {
            return true;
        }
        if (left.length() == 0 || right.length() == 0) {
            return false;
        }
        if (left.equals(right)) {
            return true;
        }
        return (left.length() == 16 && right.length() == 8
                && (left.startsWith(right) || left.endsWith(right)))
                || (right.length() == 16 && left.length() == 8
                && (right.startsWith(left) || right.endsWith(left)));
    }

    private String trimTrailingSlash(String path) {
        String clean = path == null ? "" : path.replace('\\', '/').trim();
        while (clean.length() > 1 && clean.endsWith("/")) {
            clean = clean.substring(0, clean.length() - 1);
        }
        return clean;
    }

    private String normalizeStoragePath(String path) {
        String clean = trimTrailingSlash(path);
        if (clean.equals("/sdcard") || clean.startsWith("/sdcard/")) {
            return "/storage/emulated/0" + clean.substring("/sdcard".length());
        }
        String selfPrimary = "/storage/self/primary";
        if (clean.equals(selfPrimary) || clean.startsWith(selfPrimary + "/")) {
            return "/storage/emulated/0" + clean.substring(selfPrimary.length());
        }
        return clean;
    }

    private boolean isInternalSharedStoragePath(String path) {
        String clean = trimTrailingSlash(path);
        return clean.equals("/storage/emulated/0") || clean.startsWith("/storage/emulated/0/");
    }

    private String safeRequestId(String value) {
        if (value == null || !value.matches("[0-9a-fA-F]{32}")) {
            return "";
        }
        return value.toLowerCase(Locale.US);
    }

    private String randomHex(int byteCount) {
        byte[] bytes = new byte[byteCount];
        new SecureRandom().nextBytes(bytes);
        StringBuilder result = new StringBuilder(byteCount * 2);
        for (byte value : bytes) {
            result.append(String.format(Locale.US, "%02x", value & 0xff));
        }
        return result.toString();
    }

    private String hexString(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format(Locale.US, "%02x", value & 0xff));
        }
        return result.toString();
    }

    private byte[] hexBytes(String value) {
        if (value == null || value.length() % 2 != 0) {
            throw new IllegalArgumentException("invalid session key");
        }
        byte[] result = new byte[value.length() / 2];
        for (int index = 0; index < value.length(); index += 2) {
            result[index / 2] = (byte) Integer.parseInt(value.substring(index, index + 2), 16);
        }
        return result;
    }

    private byte[] hmac(byte[] key, byte[] data) throws Exception {
        return newHmac(key).doFinal(data);
    }

    private String cleanMessage(Throwable exc) {
        String message = exc.getMessage();
        if (message == null || message.trim().length() == 0) {
            message = exc.getClass().getSimpleName();
        }
        return cleanMessageText(message);
    }

    private String cleanMessageText(String message) {
        if (message == null || message.trim().length() == 0) {
            return "P2P control operation failed";
        }
        return message.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').trim();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "OpenADB file transfers",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Temporary peer-to-peer file transfers requested by OpenADB");
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
    }

    private Notification notification(String text) {
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return builder
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setContentTitle("OpenADB P2P transfer")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    private void updateNotification(String text) {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, notification(text));
        }
    }

    private void closeQuietly(java.io.Closeable closeable) {
        if (closeable != null) {
            try {
                closeable.close();
            } catch (Throwable ignored) {
            }
        }
    }

    private final class ControlSession {
        final String requestId;
        final Object outputLock = new Object();
        final AtomicBoolean cancelled = new AtomicBoolean(false);
        final AtomicBoolean errorPublished = new AtomicBoolean(false);
        final Object resourceLock = new Object();
        volatile LocalServerSocket controlListener;
        volatile LocalSocket controlSocket;
        volatile OutputStream controlOutput;
        volatile ServerSocket dataServer;
        volatile Socket dataSocket;
        volatile Thread controlMonitor;
        volatile boolean controlAcceptExpired;
        volatile boolean controlCloseRequested;
        volatile boolean readyPublished;
        volatile boolean completed;

        ControlSession(String requestId) {
            this.requestId = requestId;
        }

        boolean isCancelled() {
            return cancelled.get();
        }

        void attachControlListener(LocalServerSocket listener) {
            synchronized (resourceLock) {
                if (cancelled.get() || completed) {
                    closeQuietly(listener);
                    return;
                }
                controlListener = listener;
            }
        }

        void attachControlSocket(LocalSocket socket) {
            synchronized (resourceLock) {
                if (cancelled.get() || completed) {
                    closeQuietly(socket);
                    return;
                }
                controlSocket = socket;
            }
        }

        void attachControlOutput(OutputStream output) {
            synchronized (resourceLock) {
                if (cancelled.get() || completed) {
                    closeQuietly(output);
                    return;
                }
                controlOutput = output;
            }
        }

        void attachDataServer(ServerSocket server) {
            synchronized (resourceLock) {
                if (cancelled.get() || completed) {
                    closeQuietly(server);
                    return;
                }
                dataServer = server;
            }
        }

        void attachDataSocket(Socket socket) {
            synchronized (resourceLock) {
                if (cancelled.get() || completed) {
                    closeQuietly(socket);
                    return;
                }
                dataSocket = socket;
            }
        }

        void cancel() {
            cancelled.set(true);
            synchronized (resourceLock) {
                closeResourcesLocked(true);
            }
        }

        void closeControlResources() {
            synchronized (resourceLock) {
                closeQuietly(controlSocket);
                closeQuietly(controlListener);
                controlSocket = null;
                controlListener = null;
                controlOutput = null;
            }
        }

        void finish() {
            completed = true;
            synchronized (resourceLock) {
                closeResourcesLocked(true);
            }
        }

        private void closeResourcesLocked(boolean includeData) {
            closeQuietly(controlSocket);
            closeQuietly(controlListener);
            controlSocket = null;
            controlListener = null;
            controlOutput = null;
            if (includeData) {
                closeQuietly(dataSocket);
                closeQuietly(dataServer);
                dataSocket = null;
                dataServer = null;
            }
        }
    }

    private static final class SessionRequest {
        final int port;
        final int timeoutSeconds;
        final String destination;
        final String bootstrapSecret;

        SessionRequest(int port, int timeoutSeconds, String destination, String bootstrapSecret) {
            this.port = port;
            this.timeoutSeconds = timeoutSeconds;
            this.destination = destination;
            this.bootstrapSecret = bootstrapSecret;
        }
    }

    private static final class SafDirectory {
        final Uri treeUri;
        final String documentId;
        final Uri documentUri;

        SafDirectory(Uri treeUri, String documentId, Uri documentUri) {
            this.treeUri = treeUri;
            this.documentId = documentId;
            this.documentUri = documentUri;
        }
    }

    private static final class DestinationAccess {
        final SafDirectory safDirectory;
        final File directDirectory;

        private DestinationAccess(SafDirectory safDirectory, File directDirectory) {
            this.safDirectory = safDirectory;
            this.directDirectory = directDirectory;
        }

        static DestinationAccess saf(SafDirectory directory) {
            return new DestinationAccess(directory, null);
        }

        static DestinationAccess direct(File directory) {
            return new DestinationAccess(null, directory);
        }
    }

    private static final class ChildDocument {
        final String documentId;
        final Uri uri;
        final String mimeType;

        ChildDocument(String documentId, Uri uri, String mimeType) {
            this.documentId = documentId;
            this.uri = uri;
            this.mimeType = mimeType == null ? "" : mimeType;
        }
    }
}
