"""Загрузка ML-моделей с кэшем в каталоге пользовательских данных.

`bge_cache_folder()`, `dir_size_bytes()`, `_sum_sizes()` — чистые (тестируются).
`bge_total_bytes()` ходит в сеть (метаданные HF) с fallback на константу.
`load_models()` грузит реальные модели и потому не юнит-тестируется.
"""
import os
import app_paths

BGE_REPO = "deepvk/USER-bge-m3"
BGE_FALLBACK_TOTAL_BYTES = 2_300_000_000


def bge_cache_folder() -> str:
    """Каталог кэша для модели эмбеддингов (в Application Support/models)."""
    app_paths.ensure_dirs()
    return str(app_paths.models_dir())


def dir_size_bytes(path) -> int:
    """Суммарный размер реальных файлов под path (рекурсивно). 0, если каталога нет.

    Симлинки пропускаются: HF-кэш хранит данные в blobs/, а в snapshots/ кладёт
    симлинки на них — иначе размер считался бы дважды (>100%)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _sum_sizes(siblings) -> int:
    """Сумма .size у файлов репозитория (HF siblings); None/отсутствие — как 0."""
    return sum((getattr(s, "size", None) or 0) for s in siblings)


def bge_total_bytes() -> int:
    """Суммарный размер файлов репозитория bge-m3 (метаданные HF).
    При ошибке/офлайне — fallback BGE_FALLBACK_TOTAL_BYTES."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(BGE_REPO, files_metadata=True)
        total = _sum_sizes(info.siblings)
        return total if total > 0 else BGE_FALLBACK_TOTAL_BYTES
    except Exception:
        return BGE_FALLBACK_TOTAL_BYTES


def load_models():
    """Загрузить и вернуть (nlp, sentence_model).

    spaCy `ru_core_news_lg` грузится из установленного пакета; `deepvk/USER-bge-m3`
    скачивается (при первом запуске) и кэшируется в bge_cache_folder().
    Тяжёлая операция — вызывать в фоне/executor'е, не на UI-потоке.
    """
    import spacy
    from sentence_transformers import SentenceTransformer

    nlp = spacy.load("ru_core_news_lg")
    sentence_model = SentenceTransformer(BGE_REPO, cache_folder=bge_cache_folder())
    return nlp, sentence_model
