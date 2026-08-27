package com.communism420.acbridge;

import android.content.Context;
import android.os.SystemClock;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructStat;
import android.util.Base64;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Field;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Official Shizuku UserService implementation used instead of the deprecated
 * text-based process API. This class deliberately has both constructors
 * supported by Shizuku v11-v13.
 */
public final class ShizukuCommandService extends IShizukuCommandService.Stub {
    private final Object lock = new Object();
    private final Map<String, Process> activeProcesses = new HashMap<String, Process>();
    private final Set<String> runningRequests = new HashSet<String>();
    private final Set<String> cancelledRequests = new HashSet<String>();

    public ShizukuCommandService() {
    }

    public ShizukuCommandService(Context ignoredContext) {
        // Shizuku v13 prefers this constructor. Its Context is intentionally
        // not retained because a UserService is not a normal app process.
    }

    @Override
    public int getRuntimeUid() {
        return Os.getuid();
    }

    @Override
    public boolean execute(String requestId, int timeoutSeconds) {
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            throw new IllegalArgumentException("Invalid request id");
        }
        synchronized (lock) {
            if (!runningRequests.add(requestId)) {
                throw new IllegalStateException("Request is already running");
            }
        }
        try {
            return executeInternal(requestId, ShizukuProtocol.boundedTimeout(timeoutSeconds));
        } finally {
            synchronized (lock) {
                activeProcesses.remove(requestId);
                runningRequests.remove(requestId);
                cancelledRequests.remove(requestId);
            }
        }
    }

    @Override
    public void cancel(String requestId) {
        if (!ShizukuProtocol.isValidRequestId(requestId)) {
            return;
        }
        Process process;
        synchronized (lock) {
            if (!runningRequests.contains(requestId)) {
                return;
            }
            cancelledRequests.add(requestId);
            process = activeProcesses.get(requestId);
        }
        if (process != null) {
            process.destroy();
        }
    }

    @Override
    public void destroy() {
        List<Process> processes;
        synchronized (lock) {
            cancelledRequests.addAll(runningRequests);
            cancelledRequests.addAll(activeProcesses.keySet());
            processes = new ArrayList<Process>(activeProcesses.values());
        }
        for (Process process : processes) {
            if (process != null) {
                terminateProcess(process, 1000L);
            }
        }
        System.exit(0);
    }

    private boolean executeInternal(String requestId, int timeoutSeconds) {
        int uid = Os.getuid();
        File requestFile = ShizukuProtocol.temporaryFile(requestId, "request");
        File stdoutFile = ShizukuProtocol.temporaryFile(requestId, "stdout");
        File stderrFile = ShizukuProtocol.temporaryFile(requestId, "stderr");
        File resultFile = ShizukuProtocol.temporaryFile(requestId, "result");
        File resultTemporary = ShizukuProtocol.temporaryFile(requestId, "result.tmp");
        File cancelFile = ShizukuProtocol.temporaryFile(requestId, "cancel");
        deletePlainFile(stdoutFile);
        deletePlainFile(stderrFile);
        deletePlainFile(resultFile);
        deletePlainFile(resultTemporary);

        long startedAt = SystemClock.elapsedRealtime();
        int exitCode = -1;
        boolean timedOut = false;
        boolean cancelled = false;
        boolean terminationFailed = false;
        String message = "Command could not be started.";
        BoundedPump stdoutPump = null;
        BoundedPump stderrPump = null;
        Process process = null;

        try {
            if (uid != 0 && uid != 2000) {
                message = "Shizuku UserService has an unsupported runtime identity.";
            } else {
                CommandRequest request = readRequest(requestFile);
                if (!requestFile.delete() && requestFile.exists()) {
                    throw new IOException("Request cleanup failed");
                }
                if (request.expectedUid != uid) {
                    message = "Shizuku identity changed; the request was rejected before execution.";
                } else if (isCancelled(requestId, cancelFile)) {
                    cancelled = true;
                    message = "Command cancelled before execution.";
                } else {
                    process = new ProcessBuilder(request.argv).redirectErrorStream(false).start();
                    process.getOutputStream().close();
                    synchronized (lock) {
                        activeProcesses.put(requestId, process);
                    }

                    stdoutPump = new BoundedPump(process.getInputStream(), stdoutFile);
                    stderrPump = new BoundedPump(process.getErrorStream(), stderrFile);
                    stdoutPump.start();
                    stderrPump.start();

                    long deadline = startedAt + timeoutSeconds * 1000L;
                    while (true) {
                        if (isCancelled(requestId, cancelFile)) {
                            cancelled = true;
                            message = "Command cancelled.";
                            break;
                        }
                        try {
                            exitCode = process.exitValue();
                            break;
                        } catch (IllegalThreadStateException stillRunning) {
                            if (SystemClock.elapsedRealtime() >= deadline) {
                                timedOut = true;
                                message = "Command timed out.";
                                break;
                            }
                            SystemClock.sleep(100L);
                        }
                    }

                    // A remote cancel can terminate the process between the
                    // cancellation check above and exitValue(). Recheck the
                    // cancellation token before classifying that exit so the
                    // result cannot be reported as an ordinary command error.
                    if (!cancelled && isCancelled(requestId, cancelFile)) {
                        cancelled = true;
                        message = "Command cancelled.";
                    }

                    if (cancelled || timedOut) {
                        terminationFailed = !terminateProcess(process, 2000L);
                        try {
                            exitCode = process.exitValue();
                        } catch (IllegalThreadStateException ignored) {
                            exitCode = -1;
                        }
                        if (terminationFailed) {
                            message += " The process did not terminate after a forced stop.";
                        }
                    } else if (exitCode == 0) {
                        message = "Command completed.";
                    } else {
                        message = "Command exited with code " + exitCode + ".";
                    }
                }
            }
        } catch (RequestFormatException invalidRequest) {
            message = "The Shizuku request file is malformed.";
            deletePlainFile(requestFile);
        } catch (Throwable failure) {
            message = "The Shizuku command could not be executed ("
                    + failure.getClass().getSimpleName() + ").";
            deletePlainFile(requestFile);
        }

        if (process != null && exitCode < 0 && !terminationFailed) {
            terminationFailed = !terminateProcess(process, 2000L);
            if (terminationFailed && !cancelled && !timedOut) {
                message = "The failed command process could not be terminated safely.";
            }
        }
        synchronized (lock) {
            activeProcesses.remove(requestId);
        }
        boolean stdoutStopped = joinPump(stdoutPump);
        boolean stderrStopped = joinPump(stderrPump);
        boolean outputFailure = !stdoutStopped
                || !stderrStopped
                || (stdoutPump != null && stdoutPump.failed)
                || (stderrPump != null && stderrPump.failed);
        outputFailure = !ensureOutputFile(stdoutFile) || outputFailure;
        outputFailure = !ensureOutputFile(stderrFile) || outputFailure;
        outputFailure = !secureForShell(stdoutFile, uid) || outputFailure;
        outputFailure = !secureForShell(stderrFile, uid) || outputFailure;
        if (outputFailure && !cancelled && !timedOut && exitCode == 0) {
            exitCode = -1;
            message = "The command finished, but its output could not be captured safely.";
        }
        long duration = Math.max(0L, SystemClock.elapsedRealtime() - startedAt);
        boolean published = stdoutStopped && stderrStopped && writeResult(
                resultTemporary,
                resultFile,
                requestId,
                exitCode,
                uid,
                timedOut,
                cancelled,
                terminationFailed,
                stdoutPump,
                stderrPump,
                duration,
                message
        );
        deletePlainFile(resultTemporary);
        return published;
    }

    private CommandRequest readRequest(File file) throws RequestFormatException {
        try {
            if (!isExactRegularFile(file)
                    || file.length() <= 0L
                    || file.length() > ShizukuProtocol.MAX_REQUEST_BYTES) {
                throw new RequestFormatException();
            }
            byte[] requestBytes = readBoundedRequest(file);
            BufferedReader reader = new BufferedReader(
                    new InputStreamReader(
                            new ByteArrayInputStream(requestBytes),
                            StandardCharsets.UTF_8
                    ),
                    8192
            );
            try {
                if (!ShizukuProtocol.REQUEST_HEADER.equals(reader.readLine())) {
                    throw new RequestFormatException();
                }
                String expectedUidLine = reader.readLine();
                if (expectedUidLine == null || !expectedUidLine.startsWith("expected_uid=")) {
                    throw new RequestFormatException();
                }
                int expectedUid;
                try {
                    expectedUid = Integer.parseInt(
                            expectedUidLine.substring("expected_uid=".length())
                    );
                } catch (NumberFormatException invalidUid) {
                    throw new RequestFormatException();
                }
                if (expectedUid != 0 && expectedUid != 2000) {
                    throw new RequestFormatException();
                }
                String countLine = reader.readLine();
                if (countLine == null || !countLine.startsWith("argv_count=")) {
                    throw new RequestFormatException();
                }
                int count;
                try {
                    count = Integer.parseInt(countLine.substring("argv_count=".length()));
                } catch (NumberFormatException invalidCount) {
                    throw new RequestFormatException();
                }
                if (count < 1 || count > ShizukuProtocol.MAX_ARGUMENT_COUNT) {
                    throw new RequestFormatException();
                }
                List<String> argv = new ArrayList<String>(count);
                for (int index = 0; index < count; index++) {
                    String argumentLine = reader.readLine();
                    if (argumentLine == null || !argumentLine.startsWith("arg_b64=")) {
                        throw new RequestFormatException();
                    }
                    byte[] decoded;
                    try {
                        decoded = Base64.decode(argumentLine.substring("arg_b64=".length()), Base64.NO_WRAP);
                    } catch (IllegalArgumentException invalidBase64) {
                        throw new RequestFormatException();
                    }
                    if (decoded.length > ShizukuProtocol.MAX_ARGUMENT_BYTES) {
                        throw new RequestFormatException();
                    }
                    String argument = strictUtf8(decoded);
                    if (argument.indexOf('\0') >= 0) {
                        throw new RequestFormatException();
                    }
                    argv.add(argument);
                }
                if (reader.readLine() != null) {
                    throw new RequestFormatException();
                }
                return new CommandRequest(expectedUid, Collections.unmodifiableList(argv));
            } finally {
                reader.close();
            }
        } catch (RequestFormatException expected) {
            throw expected;
        } catch (Throwable ignored) {
            throw new RequestFormatException();
        }
    }

    private static byte[] readBoundedRequest(File file) throws RequestFormatException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(8192);
        byte[] buffer = new byte[8192];
        try {
            FileInputStream input = new FileInputStream(file);
            try {
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read == 0) {
                        continue;
                    }
                    if (bytes.size() + read > ShizukuProtocol.MAX_REQUEST_BYTES) {
                        throw new RequestFormatException();
                    }
                    bytes.write(buffer, 0, read);
                }
            } finally {
                input.close();
            }
        } catch (RequestFormatException expected) {
            throw expected;
        } catch (IOException ignored) {
            throw new RequestFormatException();
        }
        if (bytes.size() == 0) {
            throw new RequestFormatException();
        }
        return bytes.toByteArray();
    }

    private static String strictUtf8(byte[] value) throws RequestFormatException {
        try {
            return StandardCharsets.UTF_8
                    .newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(value))
                    .toString();
        } catch (CharacterCodingException invalidUtf8) {
            throw new RequestFormatException();
        }
    }

    private boolean isCancelled(String requestId, File cancelFile) {
        synchronized (lock) {
            if (cancelledRequests.contains(requestId)) {
                return true;
            }
        }
        return isExactRegularFile(cancelFile);
    }

    private static boolean isExactRegularFile(File file) {
        try {
            StructStat stat = Os.lstat(file.getAbsolutePath());
            return OsConstants.S_ISREG(stat.st_mode)
                    && (stat.st_uid == 0 || stat.st_uid == 2000)
                    && file.getCanonicalFile().equals(file.getAbsoluteFile());
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static boolean waitForExit(Process process, long milliseconds) {
        long deadline = SystemClock.elapsedRealtime() + milliseconds;
        while (SystemClock.elapsedRealtime() < deadline) {
            try {
                process.exitValue();
                return true;
            } catch (IllegalThreadStateException ignored) {
                SystemClock.sleep(50L);
            }
        }
        return !isProcessAlive(process);
    }

    private static boolean terminateProcess(Process process, long graceMillis) {
        if (!isProcessAlive(process)) {
            return true;
        }
        process.destroy();
        if (waitForExit(process, graceMillis)) {
            return true;
        }
        int pid = processPid(process);
        if (pid > 1) {
            try {
                Os.kill(pid, OsConstants.SIGKILL);
            } catch (Throwable ignored) {
            }
        } else {
            process.destroy();
        }
        return waitForExit(process, 1000L);
    }

    private static boolean isProcessAlive(Process process) {
        try {
            process.exitValue();
            return false;
        } catch (IllegalThreadStateException running) {
            return true;
        }
    }

    private static int processPid(Process process) {
        Class<?> type = process.getClass();
        while (type != null) {
            try {
                Field field = type.getDeclaredField("pid");
                field.setAccessible(true);
                return field.getInt(process);
            } catch (Throwable ignored) {
                type = type.getSuperclass();
            }
        }
        return -1;
    }

    private static boolean joinPump(BoundedPump pump) {
        if (pump == null) {
            return true;
        }
        try {
            pump.join(5000L);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
        if (!pump.isAlive()) {
            return true;
        }
        pump.failed = true;
        pump.closeInput();
        try {
            pump.join(1000L);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
        return !pump.isAlive();
    }

    private static boolean ensureOutputFile(File file) {
        if (isExactRegularFile(file)) {
            return true;
        }
        deletePlainFile(file);
        try {
            FileOutputStream output = new FileOutputStream(file, false);
            output.close();
            return isExactRegularFile(file);
        } catch (IOException ignored) {
            return false;
        }
    }

    private static void deletePlainFile(File file) {
        // File.delete removes the directory entry itself and therefore does
        // not follow a pre-existing symbolic link at this exact derived path.
        file.delete();
    }

    private static boolean secureForShell(File file, int runtimeUid) {
        if (!file.exists()) {
            return false;
        }
        try {
            if (runtimeUid == 0) {
                Os.chown(file.getAbsolutePath(), 2000, 2000);
            }
            Os.chmod(file.getAbsolutePath(), 0600);
            StructStat stat = Os.lstat(file.getAbsolutePath());
            return OsConstants.S_ISREG(stat.st_mode)
                    && stat.st_uid == 2000
                    && file.getCanonicalFile().equals(file.getAbsoluteFile());
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static boolean writeResult(
            File resultTemporary,
            File resultFile,
            String requestId,
            int exitCode,
            int uid,
            boolean timedOut,
            boolean cancelled,
            boolean terminationFailed,
            BoundedPump stdout,
            BoundedPump stderr,
            long durationMs,
            String message
    ) {
        StringBuilder result = new StringBuilder(512);
        result.append(ShizukuProtocol.RESULT_HEADER).append('\n');
        result.append("request_id=").append(requestId).append('\n');
        String state = cancelled
                ? "cancelled"
                : (timedOut ? "timed_out" : (exitCode >= 0 ? "complete" : "failed"));
        result.append("state=").append(state).append('\n');
        result.append("exit_code=").append(exitCode).append('\n');
        result.append("uid=").append(uid).append('\n');
        result.append("mode=").append(ShizukuProtocol.modeForUid(uid)).append('\n');
        result.append("timed_out=").append(timedOut ? 1 : 0).append('\n');
        result.append("cancelled=").append(cancelled ? 1 : 0).append('\n');
        result.append("termination_failed=").append(terminationFailed ? 1 : 0).append('\n');
        result.append("stdout_truncated=").append(stdout != null && stdout.truncated ? 1 : 0).append('\n');
        result.append("stderr_truncated=").append(stderr != null && stderr.truncated ? 1 : 0).append('\n');
        result.append("stdout_bytes=").append(stdout == null ? 0L : stdout.writtenBytes).append('\n');
        result.append("stderr_bytes=").append(stderr == null ? 0L : stderr.writtenBytes).append('\n');
        result.append("duration_ms=").append(durationMs).append('\n');
        result.append("message_b64=").append(ShizukuProtocol.base64Message(message)).append('\n');
        byte[] bytes = result.toString().getBytes(StandardCharsets.UTF_8);
        deletePlainFile(resultTemporary);
        try {
            FileOutputStream output = new FileOutputStream(resultTemporary, false);
            try {
                output.write(bytes);
                output.flush();
                output.getFD().sync();
            } finally {
                output.close();
            }
            if (!secureForShell(resultTemporary, uid)) {
                deletePlainFile(resultTemporary);
                return false;
            }
            Os.rename(resultTemporary.getAbsolutePath(), resultFile.getAbsolutePath());
            boolean valid = isExactRegularFile(resultFile);
            if (!valid) {
                deletePlainFile(resultFile);
            }
            return valid;
        } catch (Throwable ignored) {
            deletePlainFile(resultTemporary);
            deletePlainFile(resultFile);
            return false;
        }
    }

    private static final class BoundedPump extends Thread {
        private final InputStream input;
        private final File outputFile;
        volatile boolean truncated;
        volatile boolean failed;
        volatile long writtenBytes;

        BoundedPump(InputStream input, File outputFile) {
            super("OpenADB-Shizuku-Output");
            this.input = input;
            this.outputFile = outputFile;
            setDaemon(true);
        }

        void closeInput() {
            try {
                input.close();
            } catch (IOException ignored) {
            }
        }

        @Override
        public void run() {
            byte[] buffer = new byte[16384];
            FileOutputStream output = null;
            try {
                try {
                    output = new FileOutputStream(outputFile, false);
                } catch (IOException openFailure) {
                    failed = true;
                }
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read == 0) {
                        continue;
                    }
                    long remaining = ShizukuProtocol.MAX_OUTPUT_BYTES - writtenBytes;
                    int writable = (int) Math.min((long) read, Math.max(0L, remaining));
                    if (writable > 0 && output != null) {
                        try {
                            output.write(buffer, 0, writable);
                            writtenBytes += writable;
                        } catch (IOException writeFailure) {
                            failed = true;
                            try {
                                output.close();
                            } catch (IOException ignored) {
                            }
                            output = null;
                        }
                    }
                    if (writable < read || output == null) {
                        truncated = true;
                    }
                }
                if (output != null) {
                    output.flush();
                    output.getFD().sync();
                }
            } catch (IOException readOrSyncFailure) {
                failed = true;
            } finally {
                try {
                    input.close();
                } catch (IOException ignored) {
                }
                if (output != null) {
                    try {
                        output.close();
                    } catch (IOException ignored) {
                    }
                }
            }
        }
    }

    private static final class RequestFormatException extends Exception {
        private static final long serialVersionUID = 1L;
    }

    private static final class CommandRequest {
        final int expectedUid;
        final List<String> argv;

        CommandRequest(int expectedUid, List<String> argv) {
            this.expectedUid = expectedUid;
            this.argv = argv;
        }
    }
}
