"""
show_manager.py — Manages shows and scene files on disk.

Directory layout:
  shows/
    <show_name>/
      show.json          ← fixture config, singer config, startup scene
      scenes/
        my_scene.json    ← individual scene files
"""

import json
import zipfile
import io
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ShowManager:

    def __init__(self, shows_dir: Path):
        self.shows_dir   = Path(shows_dir)
        self.active_show: str | None = None
        self._show_config: dict = {}
        self.shows_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================= #
    # Shows                                                               #
    # ================================================================= #

    def list_shows(self) -> list[dict]:
        shows = []
        for path in sorted(self.shows_dir.iterdir()):
            cfg_file = path / 'show.json'
            if path.is_dir() and cfg_file.exists():
                try:
                    cfg = json.loads(cfg_file.read_text())
                    shows.append({
                        'id':   path.name,
                        'name': cfg.get('name', path.name),
                    })
                except Exception as exc:
                    logger.warning(f'Could not read {cfg_file}: {exc}')
        return shows

    def load_show(self, show_id: str) -> dict:
        cfg_file = self.shows_dir / show_id / 'show.json'
        if not cfg_file.exists():
            raise FileNotFoundError(f'Show not found: {show_id}')
        config = json.loads(cfg_file.read_text())
        self.active_show  = show_id
        self._show_config = config
        # Ensure scenes directory exists
        (self.shows_dir / show_id / 'scenes').mkdir(exist_ok=True)
        logger.info(f'Loaded show: {show_id}')
        return config

    def get_active_show(self) -> dict:
        return self._show_config

    def save_show(self, show_id: str, config: dict):
        """Write an updated show config back to disk."""
        cfg_file = self.shows_dir / show_id / 'show.json'
        if not cfg_file.exists():
            raise FileNotFoundError(f'Show not found: {show_id}')
        cfg_file.write_text(json.dumps(config, indent=2))
        if show_id == self.active_show:
            self._show_config = config
        logger.info(f'Saved show config: {show_id}')


        if not self.active_show:
            raise RuntimeError('No active show')
        return self.shows_dir / self.active_show / 'scenes'

    # ================================================================= #
    # Scenes                                                              #
    # ================================================================= #

    def list_scenes(self) -> list[dict]:
        scenes = []
        for path in sorted(self._scenes_dir().glob('*.json')):
            try:
                data = json.loads(path.read_text())
                scenes.append({
                    'id':   path.stem,
                    'name': data.get('name', path.stem),
                    'steps': len(data.get('steps', [])),
                })
            except Exception as exc:
                logger.warning(f'Could not read scene {path}: {exc}')
        return scenes

    def load_scene(self, scene_id: str) -> dict:
        path = self._scenes_dir() / f'{scene_id}.json'
        if not path.exists():
            raise FileNotFoundError(f'Scene not found: {scene_id}')
        return json.loads(path.read_text())

    def save_scene(self, scene_id: str, data: dict) -> str:
        """Save (create or overwrite) a scene. Returns scene_id."""
        scene_id = _safe_id(scene_id)
        path     = self._scenes_dir() / f'{scene_id}.json'
        path.write_text(json.dumps(data, indent=2))
        logger.info(f'Saved scene: {scene_id}')
        return scene_id

    def delete_scene(self, scene_id: str):
        path = self._scenes_dir() / f'{scene_id}.json'
        if not path.exists():
            raise FileNotFoundError(f'Scene not found: {scene_id}')
        path.unlink()
        logger.info(f'Deleted scene: {scene_id}')

    def rename_scene(self, old_id: str, new_id: str):
        old = self._scenes_dir() / f'{old_id}.json'
        new = self._scenes_dir() / f'{_safe_id(new_id)}.json'
        if not old.exists():
            raise FileNotFoundError(f'Scene not found: {old_id}')
        old.rename(new)

    # ================================================================= #
    # Import / Export                                                     #
    # ================================================================= #

    def export_scene(self, scene_id: str) -> bytes:
        """Return raw JSON bytes for a single scene."""
        return (self._scenes_dir() / f'{scene_id}.json').read_bytes()

    def export_all_zip(self) -> bytes:
        """Return a zip file containing all scenes in the active show."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(self._scenes_dir().glob('*.json')):
                zf.write(path, path.name)
        return buf.getvalue()

    def import_file(self, filename: str, data: bytes) -> dict:
        """
        Import a single .json scene or a .zip of scenes.
        Returns {'imported': [...], 'skipped': [...], 'errors': [...]}
        """
        result = {'imported': [], 'skipped': [], 'errors': []}

        if filename.lower().endswith('.zip'):
            self._import_zip(data, result)
        elif filename.lower().endswith('.json'):
            self._import_json(filename, data, result)
        else:
            result['errors'].append(f'Unsupported file type: {filename}')

        return result

    def _import_json(self, filename: str, data: bytes, result: dict,
                     overwrite: bool = True):
        try:
            scene = json.loads(data.decode('utf-8'))
            # Basic validation
            if 'steps' not in scene:
                result['errors'].append(f'{filename}: missing "steps"')
                return
            scene_id = Path(filename).stem
            scene_id = _safe_id(scene_id)
            dest     = self._scenes_dir() / f'{scene_id}.json'
            if dest.exists() and not overwrite:
                result['skipped'].append(scene_id)
                return
            dest.write_bytes(data)
            result['imported'].append(scene_id)
        except Exception as exc:
            result['errors'].append(f'{filename}: {exc}')

    def _import_zip(self, data: bytes, result: dict):
        buf = io.BytesIO(data)
        try:
            with zipfile.ZipFile(buf, 'r') as zf:
                for name in zf.namelist():
                    if name.lower().endswith('.json'):
                        self._import_json(
                            name, zf.read(name), result, overwrite=True
                        )
        except zipfile.BadZipFile as exc:
            result['errors'].append(f'Bad zip file: {exc}')


def _safe_id(name: str) -> str:
    """Convert a name to a safe filename stem."""
    safe = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in name)
    return safe.strip().replace(' ', '_')[:64] or 'scene'
