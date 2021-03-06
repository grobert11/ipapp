import re

import _pytest
from _pytest.fixtures import FixtureRequest
from _pytest.main import ExitCode
from pytest_httpserver import HTTPServer

CONFTEST = '''
pytest_plugins = ["ipapp.pytest.qase.plugin"]
'''


def test_run_exists(
    testdir: '_pytest.pytester.Testdir', capsys, httpserver: HTTPServer
) -> None:
    httpserver.expect_request(re.compile("/v1/run/.+")).respond_with_json(
        {"status": True}
    )
    httpserver.expect_request(re.compile("/v1/result/.+")).respond_with_json(
        {"status": True}
    )

    testdir.makeconftest(CONFTEST)
    testdir.makepyfile(
        """
        import pytest

        @pytest.mark.case_id(1)
        def test_true_is_true():
            assert True is True
    """
    )
    testdir._method = "subprocess"

    result = testdir.runpytest(
        "--verbose",
        "-p",
        "no:pytest_qase",
        "--assert=plain",
        "--qase",
        f"--qase-url={httpserver.url_for('/')}",
        "--qase-project-id=1",
        "--qase-token=secret",
        "--qase-member-id=7",
        "--qase-run-title=exists_title",
        "--qase-jaeger-url=http://localhost:16686",
        "--qase-jaeger-service=ipapp",
    )

    result.stdout.no_fnmatch_line("*QaseWarning*")
    result.stdout.fnmatch_lines(["*::test_true_is_true PASSED*"])

    assert result.ret == ExitCode.OK

    capsys.readouterr()  # hide stdout


# olga
def test_run_not_exists(
    testdir: '_pytest.pytester.Testdir',
    request: FixtureRequest,
    capsys,
    httpserver,
) -> None:
    httpserver.expect_request(re.compile("/v1/run/.+")).respond_with_json(
        {"status": True}
    )
    httpserver.expect_request(re.compile("/v1/result/.+")).respond_with_json(
        {"status": True}
    )

    testdir.makeconftest(CONFTEST)
    testdir.makepyfile(
        """
        import pytest

        @pytest.mark.case_id(2)
        def test_true_is_false():
            assert True is False
    """
    )
    testdir._method = "subprocess"

    result = testdir.runpytest(
        "--verbose",
        "-p",
        "no:pytest_qase",
        "--assert=plain",
        "--qase",
        f"--qase-url={httpserver.url_for('')}",
        "--qase-project-id=1",
        "--qase-token=secret",
        "--qase-member-id=7",
        "--qase-run-title=not_exists_title",
        "--qase-jaeger-url=http://localhost:16686",
        "--qase-jaeger-service=ipapp",
    )

    result.stdout.no_fnmatch_line("*QaseWarning*")
    result.stdout.fnmatch_lines(["*::test_true_is_false FAILED*"])

    assert result.ret == ExitCode.TESTS_FAILED

    capsys.readouterr()  # hide stdout
