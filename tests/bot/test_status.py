from dccon2signal.pipeline import Stage
from dccon2signal_bot.status import render


def test_render_simple_stage():
    out = render(Stage.FETCHING)
    assert "디시콘 정보" in out


def test_render_with_progress():
    out = render(Stage.DOWNLOADING, progress=(12, 45))
    assert "(12/45)" in out
    assert "이미지 다운로드" in out


def test_render_with_detail_line():
    out = render(Stage.FAILED, detail="패키지 999999 를 찾을 수 없습니다")
    assert "❌" in out
    assert "999999" in out


def test_render_done_and_failed_have_distinct_prefixes():
    done = render(Stage.DONE)
    failed = render(Stage.FAILED, detail="something broke")
    assert "✅" in done
    assert "❌" in failed
