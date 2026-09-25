"""The public feature guide keeps its navigation and real images in sync."""

from html.parser import HTMLParser
from pathlib import Path

from fasthtml.common import to_xml

from web.features import FEATURES, features_page


class _FeatureMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.article_ids = set()
        self.toc_targets = set()
        self.images = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article":
            self.article_ids.add(attributes.get("id"))
        elif tag == "a" and attributes.get("href", "").startswith("#"):
            self.toc_targets.add(attributes["href"][1:])
        elif tag == "img" and attributes.get("src", "").startswith("/static/features/"):
            self.images.add(attributes["src"].removeprefix("/static/features/"))


def test_feature_toc_and_screenshots_are_complete():
    markup = _FeatureMarkup()
    markup.feed(to_xml(features_page()))
    expected_slugs = {feature.slug for feature in FEATURES}
    expected_images = {feature.image for feature in FEATURES}

    assert 15 <= len(FEATURES) <= 20
    assert len(expected_slugs) == len(FEATURES)
    assert len(expected_images) == len(FEATURES)
    assert markup.article_ids == markup.toc_targets == expected_slugs
    assert markup.images == expected_images
    assets = Path(__file__).parents[1] / "static" / "features"
    assert all((assets / image).is_file() for image in expected_images)
