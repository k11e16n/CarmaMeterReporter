"""上傳檔案到 GCS、產生 v4 簽章 URL。

支援兩種認證方式：
- 本機測試：傳 key_path（service account JSON key 檔案路徑）
- 正式部署到 VM：不傳 key_path，走 ADC（Application Default Credentials），
  用 VM 自己綁定的 service account 身份——這代表部署前要先手動幫 VM 的
  service account 在 bucket 上加好 IAM 權限，這是部署階段的事，這支模組
  本身不處理。
"""

from __future__ import annotations

from datetime import timedelta

from google.cloud import storage


def _get_client(key_path: str | None = None) -> storage.Client:
    if key_path:
        return storage.Client.from_service_account_json(key_path)
    return storage.Client()


def upload_and_sign(
    local_path: str,
    blob_name: str,
    bucket_name: str = "carmameter_bucket",
    key_path: str | None = None,
    expiration_hours: int = 72,
) -> str:
    """上傳本機檔案到 GCS，回傳一個 v4 簽章的 HTTPS URL（可直接餵給
    build_flex_bubble() 的 hero_image_url 參數）。"""
    client = _get_client(key_path)
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.upload_from_filename(local_path)
    return blob.generate_signed_url(
        version="v4", expiration=timedelta(hours=expiration_hours), method="GET"
    )
