"""Build a clean source archive for GitHub without local data or credentials."""
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

root=Path(__file__).resolve().parents[1]
out=root/'release';out.mkdir(exist_ok=True)
archive=out/'upk-github.zip'
files=['app.py','assistant.py','groq_chat.py','db.py','integrations.py','schema.sql','package.json','requirements.txt','requirements-dev.txt','README.md','.gitignore','.env.example']
folders=['static','docs','scripts','tests','.devcontainer','.github']
paths=[root/p for p in files]
for name in folders:
 paths.extend(p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='.DS_Store' and p.suffix!='.pyc')
with ZipFile(archive,'w',ZIP_DEFLATED) as z:
 for p in sorted(paths): z.write(p,p.relative_to(root))
with ZipFile(archive) as z:
 for name in z.namelist():
  assert not any(part in {'.data','.venv','.env','.git','__pycache__'} for part in Path(name).parts),name
 assert z.testzip() is None
print(f'{archive}: {len(paths)} файлов, {archive.stat().st_size:,} байт. Локальные данные и секреты исключены.')
