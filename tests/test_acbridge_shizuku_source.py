from __future__ import annotations

import hashlib
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "openadb" / "resources" / "acbridge"
SOURCE = BRIDGE / "src" / "com" / "communism420" / "acbridge"
ANDROID = "{http://schemas.android.com/apk/res/android}"


class ACBridgeShizukuSourceTests(unittest.TestCase):
    def test_official_shizuku_dependencies_are_pinned(self) -> None:
        expected = {
            "api-13.1.5.aar": "4def9bde498ef8626614c2fc5db9af4749c86f16f6c33e3f5658d35e70bab59b",
            "provider-13.1.5.aar": "b0f18cd9812464ec171c53cac93a819fe411718a3965c311f01eb4de265381b3",
            "aidl-13.1.5.aar": "33fe7191cdd69fcb66d649264f3b0c47acb2f3d6343afc05b98dbbff6f221963",
            "shared-13.1.5.aar": "4659642c9339be0a26e9c65bb8648f7ad6d8f4a465f557993ccbc78802381635",
        }
        dependency_dir = BRIDGE / "third_party" / "shizuku-13.1.5"
        for filename, digest in expected.items():
            artifact = dependency_dir / filename
            self.assertTrue(artifact.is_file(), artifact)
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), digest)
            with zipfile.ZipFile(artifact) as archive:
                self.assertEqual(archive.namelist().count("classes.jar"), 1)

    def test_min_sdk_23_core_library_desugaring_is_pinned(self) -> None:
        expected = {
            "desugar_jdk_libs-2.1.5.jar": (
                "d8044befae095781b9a80bf1faa92edc30382d75d437476784c1bf991598a976"
            ),
            "desugar_jdk_libs_configuration-2.1.5.jar": (
                "7bc9051b3a1ec19806311dcb6aa9b9ba7ef9c22caa6f4810da55bde285fb7770"
            ),
        }
        dependency_dir = BRIDGE / "third_party" / "desugar_jdk_libs-2.1.5"
        for filename, digest in expected.items():
            artifact = dependency_dir / filename
            self.assertTrue(artifact.is_file(), artifact)
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), digest)

        build_script = (ROOT / "tools" / "build_acbridge.py").read_text(encoding="utf-8")
        self.assertIn("com.android.tools.r8.L8", build_script)
        self.assertIn('"--desugared-lib"', build_script)
        self.assertIn('"--min-api",\n            "23"', build_script)

    def test_manifest_contains_official_provider_contract(self) -> None:
        root = ET.parse(BRIDGE / "AndroidManifest.xml").getroot()
        permissions = {
            item.attrib.get(f"{ANDROID}name") for item in root.findall("uses-permission")
        }
        self.assertIn("moe.shizuku.manager.permission.API_V23", permissions)
        application = root.find("application")
        self.assertIsNotNone(application)
        assert application is not None
        metadata = {
            item.attrib.get(f"{ANDROID}name"): item.attrib.get(f"{ANDROID}value")
            for item in application.findall("meta-data")
        }
        self.assertEqual(metadata.get("moe.shizuku.client.V3_SUPPORT"), "true")
        provider = next(
            item
            for item in application.findall("provider")
            if item.attrib.get(f"{ANDROID}name") == "rikka.shizuku.ShizukuProvider"
        )
        self.assertEqual(
            provider.attrib.get(f"{ANDROID}authorities"),
            "com.communism420.acbridge.shizuku",
        )
        self.assertEqual(provider.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(provider.attrib.get(f"{ANDROID}multiprocess"), "false")
        self.assertEqual(
            provider.attrib.get(f"{ANDROID}permission"),
            "android.permission.INTERACT_ACROSS_USERS_FULL",
        )
        activity = next(
            item
            for item in application.findall("activity")
            if item.attrib.get(f"{ANDROID}name") == ".ShizukuActivity"
        )
        self.assertEqual(activity.attrib.get(f"{ANDROID}exported"), "true")
        self.assertEqual(activity.attrib.get(f"{ANDROID}permission"), "android.permission.DUMP")
        self.assertNotIn(
            ".ShizukuCommandService",
            {item.attrib.get(f"{ANDROID}name") for item in application.findall("service")},
        )

    def test_user_service_uses_bounded_file_protocol_not_new_process(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        service = (SOURCE / "ShizukuCommandService.java").read_text(encoding="utf-8")
        protocol = (SOURCE / "ShizukuProtocol.java").read_text(encoding="utf-8")
        aidl = (SOURCE / "IShizukuCommandService.aidl").read_text(encoding="utf-8")
        combined = activity + service + protocol

        self.assertIn("Shizuku.bindUserService", activity)
        self.assertIn("Shizuku.unbindUserService", activity)
        self.assertIn("addBinderReceivedListenerSticky", activity)
        self.assertIn("removeBinderReceivedListener", activity)
        self.assertIn("removeRequestPermissionResultListener", activity)
        self.assertIn('"cancel".equals(operation)', activity)
        self.assertIn("cancellationMarker().isFile()", activity)
        self.assertIn("completeHostCancellation", activity)
        self.assertIn("commandService.cancel(requestId)", activity)
        self.assertNotIn("newProcess", combined)
        self.assertNotIn("Runtime.getRuntime().exec", combined)
        self.assertNotIn('getStringExtra("command")', activity)
        self.assertNotIn("android.util.Log", combined)
        self.assertIn("new ProcessBuilder(request.argv)", service)
        self.assertIn("MAX_REQUEST_BYTES = 128 * 1024", protocol)
        self.assertIn("MAX_ARGUMENT_COUNT = 32", protocol)
        self.assertIn("MAX_ARGUMENT_BYTES = 64 * 1024", protocol)
        self.assertIn("MAX_OUTPUT_BYTES = 8L * 1024L * 1024L", protocol)
        self.assertIn("[0-9a-f]{32}", protocol)
        self.assertIn("void destroy() = 16777114", aidl)
        self.assertIn("boolean execute(String requestId, int timeoutSeconds)", aidl)
        self.assertIn('startsWith("expected_uid=")', service)
        self.assertIn("request.expectedUid != uid", service)
        self.assertIn('temporaryFile(requestId, "result.tmp")', service)
        self.assertIn("Os.rename(resultTemporary.getAbsolutePath()", service)
        self.assertNotIn('completeStatus("completed"', activity)

    def test_result_and_status_protocol_fields_are_present(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        service = (SOURCE / "ShizukuCommandService.java").read_text(encoding="utf-8")
        for field in (
            "state=",
            "installed=",
            "binder=",
            "permission=",
            "uid=",
            "mode=",
            "api=",
            "message_b64=",
        ):
            self.assertIn(field, activity)
        for field in (
            "exit_code=",
            "uid=",
            "mode=",
            "timed_out=",
            "cancelled=",
            "termination_failed=",
            "stdout_truncated=",
            "stderr_truncated=",
            "message_b64=",
        ):
            self.assertIn(field, service)
        self.assertIn("HostStatusStore.KIND_SHIZUKU", activity)

    def test_passive_operations_are_noninteractive_without_backgrounding_task(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        theme = "setTheme(android.R.style.Theme_Translucent_NoTitleBar)"
        self.assertIn('passiveOperation = !"requestpermission".equals(operation)', activity)
        self.assertLess(activity.index(theme), activity.index("super.onCreate(savedInstanceState)"))
        self.assertIn("WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE", activity)
        self.assertIn("WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE", activity)
        self.assertNotIn("moveTaskToBack", activity)
        self.assertNotIn("backgroundPassiveOperation", activity)

    def test_permission_request_waits_for_foreground_and_is_idempotent(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        self.assertIn("foregroundResumed", activity)
        self.assertIn("windowHasFocus", activity)
        self.assertIn("maybeStartForegroundPermissionRequest()", activity)
        self.assertIn("setCurrentTaskExcludedFromRecents(true)", activity)
        guard = activity.index(
            "if (safePermission() == PackageManager.PERMISSION_GRANTED)"
        )
        request = activity.index(
            "Shizuku.requestPermission(REQUEST_PERMISSION_CODE)"
        )
        self.assertLess(guard, request)
        self.assertIn("finishAndRemoveTask()", activity)

    def test_permission_result_releases_host_only_after_terminal_publish(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        self.assertIn('"permission_host_request_id"', activity)
        completion = activity.split(
            "private void completeStatus(final String state", 1
        )[1].split("private void completeExecution()", 1)[0]
        self.assertLess(
            completion.index("writeStatus(state, message)"),
            completion.index("releasePermissionHost(state)"),
        )
        cancellation = completion.split("if (cancellationMarker().isFile())", 1)[1]
        self.assertIn('releasePermissionHost("cancelled")', cancellation)
        destroyed = activity.split("protected void onDestroy()", 1)[1].split(
            "private void registerListeners()", 1
        )[0]
        self.assertLess(
            destroyed.index("writeStatus("),
            destroyed.index('releasePermissionHost("activity_destroyed")'),
        )
        host_cancellation = activity.split(
            "private void completeHostCancellation()", 1
        )[1].split("private void registerActiveRequest()", 1)[0]
        self.assertIn('releasePermissionHost("cancelled")', host_cancellation)

    def test_activity_destruction_is_not_reported_as_host_cancellation(self) -> None:
        activity = (SOURCE / "ShizukuActivity.java").read_text(encoding="utf-8")
        self.assertIn('"activity_destroyed"', activity)
        self.assertNotIn(
            'writeStatus("cancelled", "The OpenADB Shizuku activity was closed',
            activity,
        )

    def test_built_apk_contains_app_and_desugared_library_dex(self) -> None:
        with zipfile.ZipFile(BRIDGE / "ACBridge.apk") as archive:
            names = archive.namelist()
        self.assertIn("classes.dex", names)
        self.assertIn("classes2.dex", names)


if __name__ == "__main__":
    unittest.main()
