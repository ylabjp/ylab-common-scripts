#!/usr/bin/env python3
"""
Google Drive ZIP downloader with dual authentication:
 - service_account mode
 - OAuth user-consent mode
"""

import io
import zipfile
from pathlib import Path
from typing import List, Optional

from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


# ---------------------------------------------------------
# AUTH HELPERS
# ---------------------------------------------------------

def build_service_service_account(creds_path: Path):
    creds = ServiceAccountCredentials.from_service_account_file(str(creds_path), scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def build_service_oauth(client_secret_path: Path, token_path: Path):
    import pickle

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
        return build("drive", "v3", credentials=creds)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, "wb") as f:
        pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def extract_folder_id(url: str) -> str:
    if "folders/" in url:
        return url.split("folders/")[1].split("?")[0]
    raise ValueError(f"Could not extract folder ID from: {url}")


def list_zip_files(service, folder_id: str) -> List[dict]:
    query = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder'"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    return [f for f in results.get("files", []) if f["name"].lower().endswith(".zip")]


def download_zip(service, file_id: str, out_path: Path):
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    out_path.write_bytes(fh.getvalue())


def extract_zip(zip_path: Path, extract_dir: Path):
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)


# ---------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------

def download_and_extract_drive_folder(
    folder_url: str,
    work_dir: Path,
    auth_mode: str = "service_account",
    service_account_json: Optional[str] = None,
    client_secret_json: Optional[str] = None,
) -> Path:

    work_dir.mkdir(parents=True, exist_ok=True)
    token_pickle = work_dir / "token.pickle"

    folder_id = extract_folder_id(folder_url)

    # ---- AUTH SELECT ----
    if auth_mode == "service_account":
        if not service_account_json:
            raise FileNotFoundError("service_account_json path not provided.")
        service = build_service_service_account(Path(service_account_json))

    elif auth_mode == "oauth":
        if not client_secret_json:
            raise FileNotFoundError("client_secret_json path not provided.")
        service = build_service_oauth(Path(client_secret_json), token_pickle)

    else:
        raise ValueError("auth_mode must be 'service_account' or 'oauth'")

    # ---- LIST ZIP FILES ----
    zip_files = list_zip_files(service, folder_id)
    if not zip_files:
        raise RuntimeError("No ZIP files found in Drive folder.")

    extracted_root = work_dir / "extracted"
    extracted_root.mkdir(exist_ok=True)

    # ---- PROCESS ZIP FILES ----
    for f in zip_files:
        zip_path = work_dir / f["name"]
        print(f"[INFO] Downloading ZIP: {f['name']}")
        download_zip(service, f["id"], zip_path)

        dataset_name = f["name"].replace(".zip", "")
        #dataset_extract_dir = extracted_root / dataset_name
        dataset_extract_dir = extracted_root
        print(f"dir Name {dataset_extract_dir}")
        dataset_extract_dir.mkdir(exist_ok=True)

        print(f"[INFO] Extracting → {dataset_extract_dir}")
        extract_zip(zip_path, dataset_extract_dir)

    print(f"[INFO] All ZIP files extracted → {extracted_root}")
    return extracted_root

