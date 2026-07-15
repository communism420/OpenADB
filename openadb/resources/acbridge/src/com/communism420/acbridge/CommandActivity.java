package com.communism420.acbridge;

/** ADB-only entry point for ACBridge operations that accept command extras. */
public final class CommandActivity extends MainActivity {
    @Override
    protected boolean acceptsBridgeCommands() {
        return true;
    }
}
