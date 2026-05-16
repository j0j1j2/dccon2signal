from dccon2signal.pipeline import Stage

_LABELS_KO: dict[Stage, str] = {
    Stage.QUEUED: "⏳ 큐 대기 중",
    Stage.FETCHING: "📥 디시콘 정보 가져오는 중...",
    Stage.DOWNLOADING: "📥 이미지 다운로드 중",
    Stage.PROCESSING: "✨ 이미지 변환 중",
    Stage.SAVING: "💾 저장 중...",
    Stage.UPLOADING: "🚀 Signal 업로드 중...",
    Stage.DONE: "✅ 완료!",
    Stage.FAILED: "❌ 실패",
}


def render(
    stage: Stage,
    progress: tuple[int, int] | None = None,
    detail: str = "",
) -> str:
    base = _LABELS_KO[stage]
    if progress is not None:
        base = f"{base} ({progress[0]}/{progress[1]})"
    if detail:
        base = f"{base}\n{detail}"
    return base
