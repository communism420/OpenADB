package com.communism420.acbridge;

/** Binder protocol implemented by the privileged Shizuku UserService. */
interface IShizukuCommandService {
    void destroy() = 16777114; // Reserved by the Shizuku UserService protocol.
    int getRuntimeUid() = 1;
    boolean execute(String requestId, int timeoutSeconds) = 2;
    void cancel(String requestId) = 3;
}
