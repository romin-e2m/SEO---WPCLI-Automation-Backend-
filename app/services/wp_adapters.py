from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeoMetaAdapter:
    plugin: str
    metadesc_key: str


def detect_seo_meta_adapter(active_plugins: list[str]) -> SeoMetaAdapter:
    """
    Plugin-aware mapping for meta description storage.
    Uses the most common meta keys for the supported plugins.
    """
    names = {p.lower() for p in (active_plugins or [])}

    # Yoast SEO
    if "wordpress-seo" in names or "yoast-seo" in names:
        return SeoMetaAdapter(plugin="yoast", metadesc_key="_yoast_wpseo_metadesc")

    # Rank Math
    if "seo-by-rank-math" in names or "rank-math" in names or "rank-math-seo" in names:
        return SeoMetaAdapter(plugin="rankmath", metadesc_key="rank_math_description")

    # SEOPress
    if "wp-seopress" in names or "seo-press" in names or "seopress" in names:
        return SeoMetaAdapter(plugin="seopress", metadesc_key="_seopress_titles_desc")

    # Fallback: no known plugin detected
    return SeoMetaAdapter(plugin="unknown", metadesc_key="_yoast_wpseo_metadesc")

