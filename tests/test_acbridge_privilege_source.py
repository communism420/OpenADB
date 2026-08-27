from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "openadb" / "resources" / "acbridge"
SOURCE = BRIDGE / "src" / "com" / "communism420" / "acbridge"
ANDROID = "{http://schemas.android.com/apk/res/android}"


class ACBridgePrivilegeSourceTests(unittest.TestCase):
    def test_privilege_activity_is_exported_only_to_dump_callers(self) -> None:
        root = ET.parse(BRIDGE / "AndroidManifest.xml").getroot()
        application = root.find("application")
        self.assertIsNotNone(application)
        assert application is not None
        activity = next(
            item
            for item in application.findall("activity")
            if item.attrib.get(f"{ANDROID}name") == ".PrivilegeActivity"
        )
        self.assertEqual(activity.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(
            activity.attrib.get(f"{ANDROID}permission"),
            "android.permission.DUMP",
        )
        self.assertIsNone(activity.attrib.get(f"{ANDROID}excludeFromRecents"))

        host = next(
            item
            for item in application.findall("activity")
            if item.attrib.get(f"{ANDROID}name") == ".PermissionHostActivity"
        )
        self.assertEqual(host.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(
            host.attrib.get(f"{ANDROID}permission"),
            "android.permission.DUMP",
        )
        self.assertIsNone(host.attrib.get(f"{ANDROID}excludeFromRecents"))
        self.assertEqual(host.attrib.get(f"{ANDROID}launchMode"), "singleTask")
        self.assertEqual(
            host.attrib.get(f"{ANDROID}taskAffinity"),
            "com.communism420.acbridge.permission",
        )
        for activity_name in (".PrivilegeActivity", ".ShizukuActivity"):
            interactive = next(
                item
                for item in application.findall("activity")
                if item.attrib.get(f"{ANDROID}name") == activity_name
            )
            self.assertEqual(
                interactive.attrib.get(f"{ANDROID}taskAffinity"),
                "com.communism420.acbridge.permission",
            )
        receiver = next(
            item
            for item in application.findall("receiver")
            if item.attrib.get(f"{ANDROID}name") == ".PermissionHostReceiver"
        )
        self.assertEqual(receiver.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(
            receiver.attrib.get(f"{ANDROID}permission"),
            "android.permission.DUMP",
        )

    def test_protocol_uses_random_request_ids_and_bounded_timeouts(self) -> None:
        protocol = (SOURCE / "PrivilegeProtocol.java").read_text(encoding="utf-8")
        self.assertIn('STATUS_HEADER = "OPENADB_BRIDGE_PRIVILEGE_STATUS 1"', protocol)
        self.assertIn('REQUEST_OPERATION = "requestprivilege"', protocol)
        self.assertIn('CANCEL_OPERATION = "cancelprivilege"', protocol)
        self.assertIn('[0-9a-f]{32}', protocol)
        self.assertIn("MIN_TIMEOUT_SECONDS = 5", protocol)
        self.assertIn("MAX_TIMEOUT_SECONDS = 300", protocol)
        self.assertIn("MAX_ROOT_OUTPUT_CHARS = 16 * 1024", protocol)
        self.assertIn("Base64.NO_WRAP", protocol)

    def test_root_probe_is_fixed_and_requires_exact_uid_zero(self) -> None:
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        self.assertIn('new ProcessBuilder("su", "-c", "id -u")', activity)
        self.assertIn('"0".equals(output.toString().trim())', activity)
        self.assertIn("exitCode == 0 && !outputTruncated", activity)
        self.assertIn("Continue draining excess output", activity)
        self.assertNotIn('getStringExtra("command")', activity)
        self.assertNotIn("Runtime.getRuntime().exec", activity)
        self.assertNotIn("/system/bin/sh", activity)

        fallback = (SOURCE / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn('new ProcessBuilder("su", "-c", "id -u")', fallback)
        self.assertIn("SystemClock.elapsedRealtime() + 120000L", fallback)
        self.assertIn('"0".equals(output.toString().trim())', fallback)
        self.assertIn("exitCode == 0", fallback)
        self.assertNotIn("return process.waitFor() == 0", fallback)

    def test_result_is_request_scoped_atomic_and_mirrored(self) -> None:
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        for field in (
            '"request_id="',
            '"backend="',
            '"state="',
            '"permission="',
            '"uid="',
            '"message_b64="',
        ):
            self.assertIn(field, activity)
        self.assertIn('"privilege_status_" + requestId + ".txt"', activity)
        self.assertIn('Environment.getExternalStorageDirectory(), ".adac"', activity)
        self.assertIn('new File(external, "openadb")', activity)
        self.assertIn("output.getFD().sync()", activity)
        self.assertIn("Os.rename(temporary.getAbsolutePath()", activity)
        writer = activity.split(
            "private void writeStatusAtomically(File directory, String contents)",
            1,
        )[1]
        before_open = writer.split("FileOutputStream output", 1)[0]
        self.assertNotIn("temporary.delete()", before_open)
        self.assertIn("ADB shell with mode 0666", activity)

    def test_privilege_status_has_a_dump_protected_app_private_channel(self) -> None:
        root = ET.parse(BRIDGE / "AndroidManifest.xml").getroot()
        application = root.find("application")
        assert application is not None
        provider = next(
            item
            for item in application.findall("provider")
            if item.attrib.get(f"{ANDROID}name") == ".HostStatusProvider"
        )
        self.assertEqual(
            provider.attrib.get(f"{ANDROID}authorities"),
            "com.communism420.acbridge.openadb.status",
        )
        self.assertEqual(provider.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(
            provider.attrib.get(f"{ANDROID}permission"),
            "android.permission.DUMP",
        )
        self.assertEqual(provider.attrib.get(f"{ANDROID}grantUriPermissions"), "false")

        store = (SOURCE / "HostStatusStore.java").read_text(encoding="utf-8")
        provider_source = (SOURCE / "HostStatusProvider.java").read_text(encoding="utf-8")
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        self.assertIn("context.getNoBackupFilesDir()", store)
        self.assertIn("HostStatusStore.KIND_PRIVILEGE", activity)
        self.assertIn("KIND_PERMISSION_HOST", store)
        self.assertIn("ParcelFileDescriptor.MODE_READ_ONLY", provider_source)
        self.assertNotIn("MODE_WRITE", provider_source)

    def test_standard_never_requests_elevated_access_and_root_is_cancellable(self) -> None:
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        self.assertIn('if ("standard".equals(backend))', activity)
        self.assertIn('"not_required"', activity)
        self.assertIn("android.os.Process.myUid()", activity)
        self.assertIn("signalActiveRequest(requestId)", activity)
        self.assertIn('"cancelled"', activity)
        self.assertIn("destroyRootProcess()", activity)
        self.assertIn("statusView.setVisibility(View.INVISIBLE)", activity)

    def test_interactive_root_waits_for_foreground_and_passive_task_is_hidden(self) -> None:
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        create = activity.split("protected void onCreate", 1)[1].split(
            "protected void onPostResume", 1
        )[0]
        self.assertNotIn("requestRootAccess();", create)
        self.assertIn("foregroundResumed", activity)
        self.assertIn("windowHasFocus", activity)
        self.assertIn("rootRequestStarted.compareAndSet(false, true)", activity)
        self.assertIn("setCurrentTaskExcludedFromRecents(true)", activity)
        self.assertIn("finishAndRemoveTask()", activity)

        host = (SOURCE / "PermissionHostActivity.java").read_text(encoding="utf-8")
        self.assertIn('OPEN_OPERATION = "open"', host)
        self.assertIn("PrivilegeProtocol.isValidRequestId(requestedId)", host)
        self.assertNotIn('getStringExtra("command")', host)
        self.assertIn("finishAndRemoveTask()", host)
        self.assertIn("foregroundResumed", host)
        self.assertIn("windowHasFocus", host)
        self.assertIn("persistedActiveRequest", host)
        self.assertIn("static void completeRequest", host)
        self.assertIn("active.finishHost(completedRequestId, reason)", host)
        self.assertIn("expectedRequestId.equals(requestId)", host)
        self.assertIn("clearPersistedRequestIfMatching", host)
        self.assertIn("terminalCloseRequestId", host)
        self.assertIn("CLOSING_REQUEST_KEY", host)
        self.assertIn("markPersistedRequestClosingIfMatching", host)
        self.assertIn("synchronized (ACTIVE_REQUEST_LOCK)", host)
        self.assertIn("HostStatusStore.KIND_PERMISSION_HOST", host)
        self.assertIn("MAX_TIMEOUT_SECONDS = 900", host)
        self.assertIn('"shizuku".equals(normalized)', host)
        self.assertNotIn("Map<String, WeakReference<PermissionHostActivity>>", host)

        destroyed = host.split("protected void onDestroy()", 1)[1].split(
            "static void completeRequest", 1
        )[0]
        self.assertIn("finished.get()", destroyed)
        self.assertIn("terminalCloseRequestId", destroyed)
        self.assertLess(
            destroyed.index("super.onDestroy()"),
            destroyed.index("clearPersistedRequestIfMatching"),
        )
        self.assertLess(
            destroyed.index("super.onDestroy()"),
            destroyed.index('publishStatus(applicationContext, closedRequestId, "closed")'),
        )
        finish_host = host.split("private void finishHost", 1)[1].split(
            "private void unregisterActiveRequest", 1
        )[0]
        self.assertIn("putString(CLOSING_REQUEST_KEY, expectedRequestId)", finish_host)
        self.assertNotIn("remove(ACTIVE_REQUEST_KEY)", finish_host)
        self.assertNotIn(
            "activeRequest = new WeakReference<PermissionHostActivity>(null)",
            finish_host,
        )

        receiver = (SOURCE / "PermissionHostReceiver.java").read_text(
            encoding="utf-8"
        )
        self.assertIn('"dismiss".equals(operation)', receiver)
        self.assertIn("PermissionHostActivity.completeRequest", receiver)
        self.assertNotIn('getStringExtra("command")', receiver)

    def test_root_terminal_result_releases_its_exact_host_after_publish(self) -> None:
        activity = (SOURCE / "PrivilegeActivity.java").read_text(encoding="utf-8")
        self.assertIn('getStringExtra(\n                "permission_host_request_id"', activity)
        complete = activity.split("private void complete(", 1)[1].split(
            "private synchronized void destroyRootProcess", 1
        )[0]
        self.assertLess(
            complete.index("writeStatus(state, permission, uid, message)"),
            complete.index("releasePermissionHost(state)"),
        )
        destroyed = activity.split("protected void onDestroy()", 1)[1].split(
            "private void requestRootAccess", 1
        )[0]
        self.assertLess(
            destroyed.index("writeStatus("),
            destroyed.index('releasePermissionHost("activity_destroyed")'),
        )


if __name__ == "__main__":
    unittest.main()
