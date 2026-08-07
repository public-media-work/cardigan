"""Tests for watch_transcripts.py file watcher script."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import the functions to test
from watch_transcripts import (
    get_queued_files,
    get_transcript_files,
    queue_file,
    run_once,
    send_heartbeat,
    watch_loop,
)


class TestGetQueuedFiles:
    """Tests for get_queued_files function."""

    @patch("watch_transcripts.httpx.get")
    def test_get_queued_files_success(self, mock_get):
        """Test successful retrieval of queued files."""
        # Mock API responses for different statuses
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs": [
                {"transcript_file": "file1.txt"},
                {"transcript_file": "file2.srt"},
            ],
            "total": 2,
        }
        mock_get.return_value = mock_response

        result = get_queued_files()

        # Should have called API for each status
        assert mock_get.call_count == 6  # 6 statuses
        assert "file1.txt" in result
        assert "file2.srt" in result
        assert len(result) == 2

    @patch("watch_transcripts.httpx.get")
    def test_get_queued_files_no_duplicates(self, mock_get):
        """Test that duplicate filenames across statuses are deduplicated."""

        # Mock API responses with duplicate filenames
        def mock_response_factory(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "jobs": [
                    {"transcript_file": "duplicate.txt"},
                ],
                "total": 1,
            }
            return response

        mock_get.side_effect = mock_response_factory

        result = get_queued_files()

        # Should only have one entry even though called 6 times
        assert len(result) == 1
        assert "duplicate.txt" in result

    @patch("watch_transcripts.httpx.get")
    def test_get_queued_files_empty_response(self, mock_get):
        """Test handling of empty queue."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jobs": [], "total": 0}
        mock_get.return_value = mock_response

        result = get_queued_files()

        assert len(result) == 0

    @patch("watch_transcripts.httpx.get")
    def test_get_queued_files_api_error(self, mock_get):
        """Test handling of API errors."""
        mock_get.side_effect = Exception("Connection error")

        result = get_queued_files()

        # Should return empty set on error
        assert len(result) == 0

    @patch("watch_transcripts.httpx.get")
    def test_get_queued_files_404_response(self, mock_get):
        """Test handling of non-200 status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = get_queued_files()

        # Should return empty set on non-200 status
        assert len(result) == 0


class TestGetTranscriptFiles:
    """Tests for get_transcript_files function."""

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_txt_and_srt(self, mock_dir):
        """Test detection of .txt and .srt files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            Path(tmpdir, "test1.txt").touch()
            Path(tmpdir, "test2.srt").touch()
            Path(tmpdir, "test3.pdf").touch()  # Should be ignored
            Path(tmpdir, "test4.doc").touch()  # Should be ignored

            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            result = get_transcript_files()

            assert len(result) == 2
            assert "test1.txt" in result
            assert "test2.srt" in result
            assert "test3.pdf" not in result

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_ignores_hidden(self, mock_dir):
        """Test that hidden files (starting with .) are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files including hidden ones
            Path(tmpdir, "normal.txt").touch()
            Path(tmpdir, ".hidden.txt").touch()
            Path(tmpdir, ".DS_Store").touch()

            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            result = get_transcript_files()

            assert len(result) == 1
            assert "normal.txt" in result
            assert ".hidden.txt" not in result

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_empty_directory(self, mock_dir):
        """Test handling of empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            result = get_transcript_files()

            assert len(result) == 0

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_directory_not_exists(self, mock_dir):
        """Test handling when directory doesn't exist."""
        mock_dir.exists.return_value = False

        result = get_transcript_files()

        assert len(result) == 0

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_sorted(self, mock_dir):
        """Test that results are sorted alphabetically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files in non-alphabetical order
            Path(tmpdir, "zebra.txt").touch()
            Path(tmpdir, "apple.txt").touch()
            Path(tmpdir, "middle.srt").touch()

            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            result = get_transcript_files()

            assert result == ["apple.txt", "middle.srt", "zebra.txt"]

    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_get_transcript_files_ignores_subdirectories(self, mock_dir):
        """Test that subdirectories are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files and subdirectory
            Path(tmpdir, "file.txt").touch()
            subdir = Path(tmpdir, "subdir")
            subdir.mkdir()
            Path(subdir, "nested.txt").touch()

            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            result = get_transcript_files()

            # Should only find the top-level file
            assert len(result) == 1
            assert "file.txt" in result


class TestQueueFile:
    """Tests for queue_file function."""

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_success(self, mock_post):
        """Test successful file queueing."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 123}
        mock_post.return_value = mock_response

        result = queue_file("test_project.txt")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["transcript_file"] == "test_project.txt"
        assert call_args[1]["json"]["project_name"] == "test_project"

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_strips_forclaude_suffix(self, mock_post):
        """Test that _ForClaude suffix is stripped from project name."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 123}
        mock_post.return_value = mock_response

        queue_file("myproject_ForClaude.txt")

        call_args = mock_post.call_args
        assert call_args[1]["json"]["project_name"] == "myproject"

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_strips_transcript_suffix(self, mock_post):
        """Test that _transcript suffix is stripped from project name."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 123}
        mock_post.return_value = mock_response

        queue_file("myproject_transcript.txt")

        call_args = mock_post.call_args
        assert call_args[1]["json"]["project_name"] == "myproject"

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_duplicate_detected(self, mock_post):
        """Test handling of duplicate file (409 Conflict)."""
        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.json.return_value = {"detail": {"existing_job_id": 42, "existing_status": "completed"}}
        mock_post.return_value = mock_response

        result = queue_file("duplicate.txt")

        assert result is False

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_force_parameter(self, mock_post):
        """Test that force parameter adds query string."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 123}
        mock_post.return_value = mock_response

        queue_file("test.txt", force=True)

        call_args = mock_post.call_args
        assert "?force=true" in call_args[0][0]

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_api_error(self, mock_post):
        """Test handling of API errors."""
        mock_post.side_effect = Exception("Connection refused")

        result = queue_file("test.txt")

        assert result is False

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_server_error(self, mock_post):
        """Test handling of non-success status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response

        result = queue_file("test.txt")

        assert result is False

    @patch("watch_transcripts.httpx.post")
    def test_queue_file_200_also_success(self, mock_post):
        """Test that both 200 and 201 are considered success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 123}
        mock_post.return_value = mock_response

        result = queue_file("test.txt")

        assert result is True


class TestRunOnce:
    """Tests for run_once function."""

    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_run_once_queues_new_files(self, mock_queued, mock_files, mock_queue):
        """Test that run_once queues only new files."""
        mock_queued.return_value = {"already_queued.txt"}
        mock_files.return_value = ["already_queued.txt", "new_file.txt", "another_new.srt"]

        run_once()

        # Should only queue the new files
        assert mock_queue.call_count == 2
        mock_queue.assert_any_call("new_file.txt")
        mock_queue.assert_any_call("another_new.srt")

    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_run_once_no_new_files(self, mock_queued, mock_files, mock_queue):
        """Test that run_once handles no new files gracefully."""
        mock_queued.return_value = {"file1.txt", "file2.txt"}
        mock_files.return_value = ["file1.txt", "file2.txt"]

        run_once()

        # Should not queue anything
        mock_queue.assert_not_called()

    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_run_once_empty_directory(self, mock_queued, mock_files, mock_queue):
        """Test run_once with empty directory."""
        mock_queued.return_value = set()
        mock_files.return_value = []

        run_once()

        # Should not queue anything
        mock_queue.assert_not_called()

    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_run_once_all_new_files(self, mock_queued, mock_files, mock_queue):
        """Test run_once when all files are new."""
        mock_queued.return_value = set()
        mock_files.return_value = ["new1.txt", "new2.txt", "new3.srt"]

        run_once()

        # Should queue all files
        assert mock_queue.call_count == 3


class TestWatchLoop:
    """Tests for watch_loop function."""

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_watch_loop_detects_new_file(self, mock_queued, mock_files, mock_queue, mock_sleep):
        """Test that watch loop detects and queues new files."""
        # Setup initial state
        mock_queued.return_value = set()

        # First call: initial files
        # Second call: new file appears
        # Third call: trigger KeyboardInterrupt
        mock_files.side_effect = [["initial.txt"], ["initial.txt", "new.txt"], KeyboardInterrupt()]

        watch_loop()

        # Should have queued the new file
        mock_queue.assert_called_once_with("new.txt")

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_watch_loop_ignores_existing_files(self, mock_queued, mock_files, mock_queue, mock_sleep):
        """Test that watch loop doesn't queue files seen initially."""
        mock_queued.return_value = set()

        # All calls return same files, then interrupt
        mock_files.side_effect = [["file1.txt"], ["file1.txt"], KeyboardInterrupt()]

        watch_loop()

        # Should not queue anything (file was seen initially)
        mock_queue.assert_not_called()

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_watch_loop_keyboard_interrupt(self, mock_queued, mock_files, mock_queue, mock_sleep):
        """Test that watch loop exits cleanly on Ctrl+C."""
        mock_queued.return_value = set()
        mock_files.side_effect = KeyboardInterrupt()

        # Should not raise exception
        watch_loop()

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_watch_loop_tracks_queued_files(self, mock_queued, mock_files, mock_queue, mock_sleep):
        """Test that watch loop includes already-queued files in seen set."""
        # Files already in queue
        mock_queued.return_value = {"queued.txt"}

        # First call: current files
        # Second call: queued file reappears (shouldn't queue again)
        # Third call: interrupt
        mock_files.side_effect = [["queued.txt"], ["queued.txt"], KeyboardInterrupt()]

        watch_loop()

        # Should not queue the already-queued file
        mock_queue.assert_not_called()

    @patch("watch_transcripts.POLL_INTERVAL", 1)
    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files")
    @patch("watch_transcripts.get_queued_files")
    def test_watch_loop_sleep_interval(self, mock_queued, mock_files, mock_queue, mock_sleep):
        """Test that watch loop sleeps between iterations."""
        mock_queued.return_value = set()

        # The initial startup scan consumes one get_transcript_files() call before the
        # loop; then two full iterations (each sleeps), then interrupt on the third.
        mock_files.side_effect = [[], [], [], KeyboardInterrupt()]

        watch_loop()

        # Should have slept twice (once per iteration)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(1)


class TestIntegration:
    """Integration-style tests."""

    @patch("watch_transcripts.httpx.post")
    @patch("watch_transcripts.httpx.get")
    @patch("watch_transcripts.TRANSCRIPTS_DIR")
    def test_full_workflow_once_mode(self, mock_dir, mock_get, mock_post):
        """Test full workflow in --once mode."""
        # Setup filesystem
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "new_file.txt").touch()
            Path(tmpdir, "already_queued.srt").touch()

            mock_dir_path = Path(tmpdir)
            mock_dir.exists.return_value = True
            mock_dir.iterdir.return_value = mock_dir_path.iterdir()

            # Mock API responses
            # get_queued_files() calls
            mock_get_response = MagicMock()
            mock_get_response.status_code = 200
            mock_get_response.json.return_value = {"jobs": [{"transcript_file": "already_queued.srt"}], "total": 1}
            mock_get.return_value = mock_get_response

            # queue_file() calls
            mock_post_response = MagicMock()
            mock_post_response.status_code = 201
            mock_post_response.json.return_value = {"id": 1}
            mock_post.return_value = mock_post_response

            # Run once
            run_once()

            # Should only queue the new file
            assert mock_post.call_count == 1
            call_args = mock_post.call_args
            assert call_args[1]["json"]["transcript_file"] == "new_file.txt"


class TestHeartbeatRestart:
    """send_heartbeat reports the restart flag; watch_loop exits on it (#304)."""

    @patch("watch_transcripts.httpx.post")
    def test_send_heartbeat_returns_true_when_restart_requested(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"restart": True})
        assert send_heartbeat() is True
        # posts its own boot time so the API can compute the flag
        _, kwargs = mock_post.call_args
        assert "started_at" in kwargs["json"]

    @patch("watch_transcripts.httpx.post")
    def test_send_heartbeat_returns_false_when_no_restart(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"restart": False})
        assert send_heartbeat() is False

    @patch("watch_transcripts.httpx.post", side_effect=Exception("network"))
    def test_send_heartbeat_returns_false_on_error(self, mock_post):
        assert send_heartbeat() is False

    @patch("watch_transcripts.time.sleep")
    @patch("watch_transcripts.queue_file")
    @patch("watch_transcripts.get_transcript_files", return_value=["new.srt"])
    @patch("watch_transcripts.get_queued_files", return_value=set())
    @patch("watch_transcripts.send_heartbeat", return_value=True)
    def test_watch_loop_exits_when_restart_requested(self, _hb, _q, _f, mock_queue, mock_sleep):
        # Returns (no infinite loop) because the heartbeat asked to restart —
        # and exits BEFORE processing files or sleeping.
        watch_loop()
        mock_queue.assert_not_called()
        mock_sleep.assert_not_called()
