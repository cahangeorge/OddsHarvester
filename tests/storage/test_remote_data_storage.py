import json
from unittest.mock import MagicMock, mock_open, patch

from botocore.exceptions import BotoCoreError, NoCredentialsError
import pytest

from oddsharvester.storage.remote_data_storage import RemoteDataStorage


@pytest.fixture
def remote_data_storage(monkeypatch):
    monkeypatch.setenv("OH_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("OH_AWS_REGION", "eu-west-3")
    with patch("oddsharvester.storage.remote_data_storage.boto3.client", return_value=MagicMock()):
        return RemoteDataStorage()


@pytest.fixture
def sample_data():
    return [{"team": "Team A", "odds": 2.5}, {"team": "Team B", "odds": 1.8}]


def test_initialization(remote_data_storage):
    assert remote_data_storage.s3_client is not None
    assert remote_data_storage.logger is not None
    assert remote_data_storage.S3_BUCKET_NAME == "test-bucket"
    assert remote_data_storage.AWS_REGION == "eu-west-3"


@pytest.mark.parametrize("missing", ["OH_S3_BUCKET", "OH_AWS_REGION"])
def test_missing_config_fails_before_creating_client(monkeypatch, missing):
    monkeypatch.setenv("OH_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("OH_AWS_REGION", "eu-west-3")
    monkeypatch.delenv(missing)

    with patch("oddsharvester.storage.remote_data_storage.boto3.client") as mock_client:
        with pytest.raises(ValueError, match=missing):
            RemoteDataStorage()

    mock_client.assert_not_called()


def test_env_var_override_bucket(monkeypatch):
    monkeypatch.setenv("OH_S3_BUCKET", "my-custom-bucket")
    monkeypatch.setenv("OH_AWS_REGION", "eu-west-3")
    with patch("oddsharvester.storage.remote_data_storage.boto3.client", return_value=MagicMock()):
        storage = RemoteDataStorage()

    assert storage.S3_BUCKET_NAME == "my-custom-bucket"


def test_env_var_override_region(monkeypatch):
    monkeypatch.setenv("OH_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("OH_AWS_REGION", "us-east-1")
    with patch("oddsharvester.storage.remote_data_storage.boto3.client", return_value=MagicMock()):
        storage = RemoteDataStorage()

    assert storage.AWS_REGION == "us-east-1"


def test_save_to_json(remote_data_storage, sample_data):
    mock_file = mock_open()

    with patch("builtins.open", mock_file):
        remote_data_storage._save_to_json(sample_data, "test_data.json")

    # Check if file was opened in write mode
    mock_file.assert_called_once_with("test_data.json", "w", encoding="utf-8")

    # Validate JSON content
    handle = mock_file()
    json.dump(sample_data, handle, indent=4)
    handle.write.assert_called()


def test_save_to_json_error(remote_data_storage, sample_data):
    with (
        patch("builtins.open", side_effect=OSError("File write error")),
        patch.object(remote_data_storage.logger, "error") as mock_logger,
    ):
        with pytest.raises(OSError, match="File write error"):
            remote_data_storage._save_to_json(sample_data, "test_data.json")

    mock_logger.assert_called()


def test_upload_to_s3_success(remote_data_storage):
    with patch.object(remote_data_storage.s3_client, "upload_file") as mock_upload:
        remote_data_storage._upload_to_s3("test_data.json", "s3_object.json")

    mock_upload.assert_called_once_with("test_data.json", remote_data_storage.S3_BUCKET_NAME, "s3_object.json")


def test_upload_to_s3_default_object_name(remote_data_storage):
    with patch.object(remote_data_storage.s3_client, "upload_file") as mock_upload:
        remote_data_storage._upload_to_s3("test_data.json")

    mock_upload.assert_called_once_with("test_data.json", remote_data_storage.S3_BUCKET_NAME, "test_data.json")


def test_upload_to_s3_error(remote_data_storage):
    with (
        patch.object(remote_data_storage.s3_client, "upload_file", side_effect=BotoCoreError),
        patch.object(remote_data_storage.logger, "error") as mock_logger,
    ):
        with pytest.raises(BotoCoreError):
            remote_data_storage._upload_to_s3("test_data.json", "s3_object.json")

    mock_logger.assert_called()


def test_upload_to_s3_no_credentials(remote_data_storage):
    with (
        patch.object(remote_data_storage.s3_client, "upload_file", side_effect=NoCredentialsError()),
        patch.object(remote_data_storage.logger, "error") as mock_logger,
    ):
        with pytest.raises(NoCredentialsError):
            remote_data_storage._upload_to_s3("test_data.json", "s3_object.json")

    mock_logger.assert_called()


def test_process_and_upload(remote_data_storage, sample_data):
    with (
        patch.object(remote_data_storage, "_save_to_json") as mock_save_json,
        patch.object(remote_data_storage, "_upload_to_s3") as mock_upload_s3,
    ):
        remote_data_storage.process_and_upload(sample_data, "test_data.json", "s3_object.json")

    mock_save_json.assert_called_once_with(data=sample_data, file_name="test_data.json")
    mock_upload_s3.assert_called_once_with(file_name="test_data.json", object_name="s3_object.json")


def test_process_and_upload_error(remote_data_storage, sample_data):
    with (
        patch.object(remote_data_storage, "_save_to_json", side_effect=OSError("File save error")),
        patch.object(remote_data_storage.logger, "error") as mock_logger,
    ):
        with pytest.raises(OSError, match="File save error"):
            remote_data_storage.process_and_upload(sample_data, "test_data.json", "s3_object.json")

    mock_logger.assert_called()
