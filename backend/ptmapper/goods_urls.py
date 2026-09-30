"""The goods-v1 PT surface, under ``/api/goods-v1/ptmapper/``.

Three parts: the governed vocabulary (E226-E231), the PT documents themselves
(E098-E133), and their label print jobs (E182-E184). All three used to share
paths or OpenAPI path templates with the legacy PT screens; they no longer do
(GSA-T01).
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path("", include("ptmapper.soh_urls")),
    path("", include("ptmapper.goods_vocab_urls")),
    path("", include("ptmapper.goods_pt_urls")),
    path("", include("ptmapper.goods_print_urls")),
    path("", include("ptmapper.goods_manifest_urls")),
]
