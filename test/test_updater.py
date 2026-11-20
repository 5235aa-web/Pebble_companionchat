# -*- coding: utf-8 -*-
"""GitHub Releases updater: semver ordering, asset picking, URL allowlist."""

import unittest

from backend import updater


class VersionCompareTest(unittest.TestCase):
    def test_detects_newer_patch_minor_major(self):
        self.assertTrue(updater.is_newer_version('v0.1.1', '0.1.0'))
        self.assertTrue(updater.is_newer_version('0.2.0', '0.1.9'))
        self.assertTrue(updater.is_newer_version('v0.10.0', '0.2.0'))
        self.assertTrue(updater.is_newer_version('1.0.0', '0.99.99'))

    def test_same_or_older_version_is_not_newer(self):
        self.assertFalse(updater.is_newer_version('v0.1.0', '0.1.0'))
        self.assertFalse(updater.is_newer_version('0.0.9', '0.1.0'))

    def test_stable_outranks_prerelease_of_same_number(self):
        self.assertTrue(updater.is_newer_version('0.2.0', '0.2.0-rc.1'))
        self.assertFalse(updater.is_newer_version('0.2.0-rc.1', '0.2.0'))

    def test_reads_workspace_version_file(self):
        self.assertRegex(updater.get_current_version(), r'^\d+\.\d+\.\d+')


class AssetSelectionTest(unittest.TestCase):
    def test_prefers_setup_exe_over_other_files(self):
        assets = [
            {
                'name': 'latest.json',
                'browser_download_url': 'https://github.com/a/b/releases/download/v1/latest.json',
            },
            {
                'name': 'helper.exe',
                'browser_download_url': 'https://github.com/a/b/releases/download/v1/helper.exe',
            },
            {
                'name': 'Pebble_0.1.1_x64-setup.exe',
                'browser_download_url': 'https://github.com/a/b/releases/download/v1/Pebble_0.1.1_x64-setup.exe',
            },
        ]
        picked = updater.select_windows_asset(assets)
        self.assertIsNotNone(picked)
        self.assertEqual(picked['name'], 'Pebble_0.1.1_x64-setup.exe')

    def test_falls_back_to_msi_when_no_exe(self):
        assets = [
            {
                'name': 'Pebble_0.1.1_x64.msi',
                'browser_download_url': 'https://github.com/a/b/releases/download/v1/Pebble_0.1.1_x64.msi',
            },
        ]
        picked = updater.select_windows_asset(assets)
        self.assertIsNotNone(picked)
        self.assertEqual(picked['name'], 'Pebble_0.1.1_x64.msi')

    def test_returns_none_when_no_installer_asset(self):
        assets = [
            {
                'name': 'source.tar.gz',
                'browser_download_url': 'https://github.com/a/b/releases/download/v1/source.tar.gz',
            },
        ]
        self.assertIsNone(updater.select_windows_asset(assets))


class DownloadSecurityTest(unittest.TestCase):
    def test_allows_github_https_hosts(self):
        self.assertTrue(
            updater.is_allowed_download_url(
                'https://github.com/5235aa-web/Pebble_companionchat/releases/download/v0.1.1/app-setup.exe'
            )
        )
        self.assertTrue(
            updater.is_allowed_download_url(
                'https://objects.githubusercontent.com/github-production-release-asset/123'
            )
        )

    def test_rejects_http_or_third_party_hosts(self):
        self.assertFalse(updater.is_allowed_download_url('http://github.com/a/b/setup.exe'))
        self.assertFalse(updater.is_allowed_download_url('https://evil.example.com/setup.exe'))
        self.assertFalse(updater.is_allowed_download_url('file:///C:/setup.exe'))


if __name__ == '__main__':
    unittest.main()
