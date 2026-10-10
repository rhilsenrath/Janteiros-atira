"""Read one model-selected download/document; never discover links or crawl websites."""
from io import BytesIO
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree
from pypdf import PdfReader

MAX_BYTES = 12 * 1024 * 1024

def read_document(data: bytes, name: str, member: str | None = None) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError('Selected document exceeds size limit')
    suffix = PurePosixPath(name).suffix.lower()
    if data.startswith(b'%PDF'):
        reader = PdfReader(BytesIO(data))
        if len(reader.pages) > 200:
            raise ValueError('Selected PDF exceeds page limit')
        pages = [page.extract_text() or "" for page in reader.pages]
        text = '\n'.join(f'Page {i+1}\n{page}' for i,page in enumerate(pages))
        return {'name': name, 'text': text, 'scanned_pdf': not any(page.strip() for page in pages)}
    if zipfile.is_zipfile(BytesIO(data)):
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if len(archive.infolist()) > 500:
                raise ValueError('Archive exceeds entry limit')
            if suffix == '.docx':
                info = archive.getinfo('word/document.xml')
                if info.file_size > MAX_BYTES: raise ValueError('Document XML exceeds limit')
                root = ElementTree.fromstring(archive.read(info))
                return {'name': name, 'text': '\n'.join(n.text for n in root.iter() if n.tag.endswith('}t') and n.text)}
            entries = [i.filename for i in archive.infolist() if not i.is_dir()]
            if member is None:
                return {'name': name, 'members': entries}
            if member not in entries or PurePosixPath(member).is_absolute() or '..' in PurePosixPath(member).parts:
                raise ValueError('Model must select a listed safe archive member')
            info = archive.getinfo(member)
            if info.file_size > MAX_BYTES or info.compress_size == 0 or info.file_size / info.compress_size > 200:
                raise ValueError('Unsafe compressed document size')
            return read_document(archive.read(info), member)
    if suffix in ('.txt','.csv','.tsv','.html','.htm','.xml','.json','.md'):
        text = data.decode('utf-8-sig', errors='replace')
        if suffix in ('.html','.htm'):
            from html.parser import HTMLParser
            class Text(HTMLParser):
                def __init__(self): super().__init__(); self.parts=[]
                def handle_data(self, value): self.parts.append(value)
            parser=Text(); parser.feed(text); text='\n'.join(parser.parts)
        return {'name': name, 'text': text}
    raise ValueError('Selected document format is unsupported; report missing information')
