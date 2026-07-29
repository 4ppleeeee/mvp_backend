from pathlib import Path

from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.ingestion.article import ArticleContentParser, FetchedHtml
from app.ingestion.domain import EvidenceBundle, EvidenceOrigin, MediaMetadata, MediaType, Transcript, TranscriptSegment
from app.ingestion.service import IngestionService
from app.llm import SourceAnalysis
from app.models import IngestionJob, SourceEvidence, TravelSource


def test_article_content_parser_extracts_xhs_public_h5_note() -> None:
    html = """
    <html><head><title>小红书</title></head><body><script>
    window.__INITIAL_STATE__={
      "note": {"noteDetailMap": {"66abc": {"note": {
        "title": "三亚亲子旅行路线",
        "desc": "海边酒店、免税店和椰梦长廊安排。",
        "imageList": [{"urlDefault": "https://img.example/xhs-cover.jpg"}]
      }}}}
    };
    </script></body></html>
    """

    content = ArticleContentParser().parse("https://www.xiaohongshu.com/explore/66abc", html)

    assert content.platform == "xiaohongshu"
    assert content.title == "三亚亲子旅行路线"
    assert content.body_text == "海边酒店、免税店和椰梦长廊安排。"
    assert content.cover_image_url == "https://img.example/xhs-cover.jpg"


def test_article_content_parser_detects_xhs_video_page() -> None:
    html = '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"abc":{"note":{"video":{"url":"https://sns-video-v6.xhscdn.com/clip.mp4"}}}}}};</script>'

    assert ArticleContentParser.is_xhs_video_html(html)


def test_article_content_parser_probes_xhs_video_url_without_network() -> None:
    class FakeFetcher:
        def fetch(self, url: str) -> FetchedHtml:
            return FetchedHtml(url=url, html='<video src="https://sns-video-v6.xhscdn.com/clip.mp4"></video>')

    assert ArticleContentParser.is_xhs_video_url(
        "https://www.xiaohongshu.com/discovery/item/abc",
        fetcher=FakeFetcher(),
    )


def test_article_content_parser_uses_open_graph_as_generic_fallback() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="京都三日路线">
      <meta name="description" content="清水寺、岚山与咖啡店安排。">
      <meta property="og:image" content="http://img.example/kyoto.jpg">
    </head><body><p>ignored body</p></body></html>
    """

    content = ArticleContentParser().parse("https://example.com/kyoto", html)

    assert content.platform == "web"
    assert content.title == "京都三日路线"
    assert content.body_text == "清水寺、岚山与咖啡店安排。"
    assert content.cover_image_url == "https://img.example/kyoto.jpg"


class TravelLlm:
    async def analyze_source(self, **_: object) -> SourceAnalysis:
        return SourceAnalysis(
            is_travel_related=True,
            confidence=0.9,
            destination="三亚",
            category="guide",
            location_name=None,
            normalized_tags=["亲子"],
            raw_tags=["路线"],
        )


class ArticlePipeline:
    media_egress = "router_default"

    def extract(self, url: str, _: str) -> EvidenceBundle:
        return EvidenceBundle(
            metadata=MediaMetadata(title="三亚亲子旅行路线", source_platform="xiaohongshu", canonical_url=url),
            transcript=Transcript(
                language="zh",
                origin=EvidenceOrigin.ARTICLE,
                full_text="海边酒店、免税店和椰梦长廊安排。",
                segments=(TranscriptSegment(start_seconds=0, end_seconds=0, text="海边酒店、免税店和椰梦长廊安排。"),),
                media_type=MediaType.ARTICLE,
            ),
        )


def test_ingestion_service_persists_article_evidence_as_article(tmp_path: Path) -> None:
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'article.db'}"))
    init_db(engine)
    with Session(engine) as session:
        job = IngestionJob(
            input_type="url",
            original_url="https://www.xiaohongshu.com/explore/66abc",
            source_platform="xiaohongshu",
            media_type="article",
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        result = IngestionService(session=session, llm_client=TravelLlm(), pipeline=ArticlePipeline()).run(job.job_id)

        source = session.exec(select(TravelSource).where(TravelSource.source_id == result.source_id)).one()
        evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == source.source_id)).one()
        assert result.status == "succeeded"
        assert result.media_type == "article"
        assert evidence.origin == "article"
