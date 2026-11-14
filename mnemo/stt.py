"""--stt: speech-to-text, writing FILE.txt next to every audio FILE under the sources.

faster-whisper and ctranslate2 are imported lazily, inside the functions that need them, because
they only exist in the uv venv (REPO/.venv), not in the system python that runs everything else.
"""
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, asdict

from mnemo import DATA_DIR, logger, get_env, parse_bool, format_elapsed


STT_EXTENSIONS = ('.m4a', '.aac', '.amr')

STT_VENV = os.path.normpath(os.path.join(DATA_DIR, ".venv"))

# Preferred compute types, best first, used when the requested one isn't supported by the device
STT_COMPUTE_FALLBACK = ("float16", "int8_float16", "int8_float32", "int8", "float32")


def stt_cuda_lib_dirs():
    """Directories of the CUDA/cuDNN runtime wheels installed in the stt venv, if any."""
    import glob
    return sorted(glob.glob(os.path.join(STT_VENV, "lib", "python*", "site-packages", "nvidia", "*", "lib")))


def ensure_stt_runtime(script):
    """Re-exec `script` (driver.py) under the stt venv's python, with its CUDA libs on LD_LIBRARY_PATH.

    faster-whisper is the only non-stdlib dependency, so it lives in REPO/.venv (built by
    driver.sh's mnemo_stt_setup) rather than the system python. LD_LIBRARY_PATH is only read
    at process start, hence the re-exec instead of setting it in-process. Must run before
    anything is logged: the empty log file of this process is removed, the child opens its own.
    """
    venv_python = os.path.join(STT_VENV, "bin", "python")
    if not os.path.isfile(venv_python):
        logger.error(f"No stt environment at {STT_VENV}. Create it with: source driver.sh && mnemo_stt_setup")
        sys.exit(1)
    if os.environ.get("MNEMO_STT_REEXEC") == "1":
        return  # already re-executed once, don't loop

    lib_dirs = stt_cuda_lib_dirs()
    current = [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(":") if d]
    in_venv = os.path.realpath(sys.prefix) == os.path.realpath(STT_VENV)
    if in_venv and all(d in current for d in lib_dirs):
        return

    env = dict(os.environ, MNEMO_STT_REEXEC="1", LD_LIBRARY_PATH=":".join(lib_dirs + current))
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            logger.removeHandler(h)
            try:
                if os.path.getsize(h.baseFilename) == 0:
                    os.remove(h.baseFilename)
            except OSError:
                pass
    sys.stdout.flush()
    os.execve(venv_python, [venv_python, script] + sys.argv[1:], env)


def stt_txt_path(audio_path):
    return audio_path + ".txt"


def find_audio_files(src):
    """Every file under `src` with an STT extension (case-insensitive), sorted."""
    found = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(STT_EXTENSIONS):
                found.append(os.path.join(root, name))
    return found


@dataclass(frozen=True)
class WhisperSettings:
    """The resolved WhisperModel parameters, after defaults and GPU/compute-type fallbacks."""
    model: str
    device: str
    compute_type: str
    num_workers: int
    cpu_threads: int


def load_whisper_model():
    """Build the WhisperModel from MNEMO_* variables, using the GPU when possible.

    Returns (model, WhisperSettings). The GPU path is validated with a short warm-up transcription,
    because missing CUDA/cuDNN libraries only surface at inference time; on any GPU failure
    the model is rebuilt on the CPU.
    """
    import ctranslate2
    from faster_whisper import WhisperModel

    cpu_count = os.cpu_count() or 1
    model_name = get_env("MODEL", "small")
    device = get_env("DEVICE", "auto").lower()
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    if device not in ("cuda", "cpu"):
        logger.error(f"MNEMO_DEVICE must be auto, cuda or cpu, not {device!r}")
        sys.exit(1)

    def build(d):
        requested = get_env("COMPUTE_TYPE", "float16" if d == "cuda" else "int8")
        supported = ctranslate2.get_supported_compute_types(d)
        compute_type = requested
        if requested != "default" and requested not in supported:
            compute_type = next((c for c in STT_COMPUTE_FALLBACK if c in supported), "default")
            logger.warning(f"Compute type {requested} is not supported on {d} "
                           f"(supported: {', '.join(sorted(supported))}), using {compute_type}")
        num_workers = max(1, get_env("NUM_WORKERS", 2 if d == "cuda" else max(1, cpu_count // 4), int))
        cpu_threads = get_env("CPU_THREADS", max(1, cpu_count // num_workers), int)
        s = WhisperSettings(model_name, d, compute_type, num_workers, cpu_threads)
        logger.info("Loading Whisper model: " + ", ".join(f"{k}={v}" for k, v in asdict(s).items()))
        m = WhisperModel(s.model, device=s.device, compute_type=s.compute_type,
                         num_workers=s.num_workers, cpu_threads=s.cpu_threads)
        return m, s

    if device == "cuda":
        try:
            model, settings = build("cuda")
            import numpy
            segments, _ = model.transcribe(numpy.zeros(16000, dtype=numpy.float32), language="en", beam_size=1)
            list(segments)
            return model, settings
        except Exception as e:
            logger.warning(f"GPU unusable ({e}), falling back to CPU")
    return build("cpu")


def stt(sources, dry_run):
    """Write FILE.txt next to every audio FILE under the sources, transcribed with faster-whisper.

    Files whose .txt already exists are skipped, so an interrupted run can be resumed. Each
    transcript is written to a hidden temp file and renamed into place, so a .txt is never partial.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_time = time.time()
    todo, already = [], 0
    for src in sources:
        files = find_audio_files(src)
        pending = [f for f in files if not os.path.exists(stt_txt_path(f))]
        already += len(files) - len(pending)
        logger.info(f"{src}: {len(files)} audio files, {len(pending)} to transcribe")
        todo.extend(pending)

    if dry_run:
        for f in todo:
            logger.info(f"[DRY-RUN] Would write {stt_txt_path(f)}")
        logger.info(f"[DRY-RUN] Would transcribe {len(todo)} files, {already} already have a .txt")
        logger.info(f"stt elapsed time: {format_elapsed(start_time)}")
        return
    if not todo:
        logger.info(f"Nothing to transcribe, {already} files already have a .txt")
        return

    language = get_env("LANGUAGE", None)
    beam_size = get_env("BEAM_SIZE", 5, int)
    vad_filter = get_env("VAD_FILTER", True, parse_bool)
    model, settings = load_whisper_model()
    logger.info(f"Transcribing {len(todo)} files: language={language or 'auto'}, "
                f"beam_size={beam_size}, vad_filter={vad_filter}")

    in_flight = set()  # temp files being written, removed on interrupt

    def transcribe_one(path):
        t0 = time.time()
        segments, info = model.transcribe(path, language=language, beam_size=beam_size, vad_filter=vad_filter)
        lines = [s.text.strip() for s in segments if s.text.strip()]
        target = stt_txt_path(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(target)}.", suffix=".part")
        in_flight.add(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        finally:
            in_flight.discard(tmp)
            if os.path.exists(tmp):
                os.remove(tmp)
        return info.language, info.language_probability, info.duration, time.time() - t0

    done = failed = 0
    executor = ThreadPoolExecutor(max_workers=settings.num_workers)
    try:
        futures = {executor.submit(transcribe_one, f): f for f in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            path = futures[fut]
            try:
                lang, prob, duration, took = fut.result()
                done += 1
                logger.info(f"[{i}/{len(todo)}] {stt_txt_path(path)} "
                            f"({lang} {prob:.0%}, {duration:.0f}s audio in {took:.0f}s)")
            except Exception as e:
                failed += 1
                logger.error(f"[{i}/{len(todo)}] Failed {path}: {e}")
    except KeyboardInterrupt:
        print()
        logger.critical(f"Transcription interrupted by user after {done} files. Rerun to resume.")
        executor.shutdown(wait=False, cancel_futures=True)
        for tmp in list(in_flight):
            try:
                os.remove(tmp)
            except OSError:
                pass
        logger.info(f"stt elapsed time: {format_elapsed(start_time)}")
        os._exit(1)  # worker threads are stuck in native code; don't wait for them
    executor.shutdown()

    logger.info(f"Transcribed {done} files, {failed} failed, {already} already had a .txt")
    logger.info(f"stt elapsed time: {format_elapsed(start_time)}")
    if failed:
        sys.exit(1)
