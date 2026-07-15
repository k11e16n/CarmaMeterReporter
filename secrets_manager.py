#!/usr/bin/env python3
"""
從 GCP Secret Manager 讀取 CarmaMeterReporter 的憑證設定。

設計成「一個專案一包」：所有憑證打包成單一個 JSON secret，而不是每個
憑證各自建一個 secret——Secret Manager 的免費額度是算「secret 版本
數」，不是算「憑證數量」，包成一包可以讓一個免費額度撐起整個專案。
之後新增股票、新聞模組時，各自也用同樣的方式包一個 secret，而不是
每加一個憑證就多佔一份免費額度。

用法：
    from secrets_manager import load_config

    config = load_config(project_id="your-gcp-project")
    config["carma_service_address"]
    config["line_channel_access_token"]
    ...

依賴：
    pip install google-cloud-secret-manager --break-system-packages
"""

from __future__ import annotations

import json

from google.cloud import secretmanager

DEFAULT_SECRET_ID = "carmameterreporter-config"


def load_config(project_id: str, secret_id: str = DEFAULT_SECRET_ID, version: str = "latest") -> dict:
    """
    讀取並解析 JSON 格式的 secret，回傳 dict。

    失敗（權限不足、secret 不存在、內容不是合法 JSON）一律讓例外往上拋，
    不在這裡吞掉——憑證讀不到，程式本來就不該假裝沒事繼續跑，應該讓
    呼叫端的錯誤處理機制（exit code、log）接手。
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")
    return json.loads(payload)
