import pytest
from app.schemas.review import ParsedFile,ParsedPage,DocumentBlock
from app.services.file_parser import parse_text,ParseError
from app.services.evidence_catalog import catalog_for


@pytest.mark.parametrize('encoding',['utf-8-sig','utf-16','gb18030'])
def test_text_decoding_preserves_formula_conditions_and_specialist_terms(tmp_path,encoding):
    content='条件概率 P(A|B)=P(A∩B)/P(B)，条件：P(B)>0。'
    path=tmp_path/'course.txt';path.write_bytes(content.encode(encoding))
    assert parse_text(path)[0].text==content


def test_invalid_text_never_silently_discards_bytes(tmp_path):
    path=tmp_path/'course.txt';path.write_bytes(b'formula\xff')
    with pytest.raises(ParseError):parse_text(path)


def test_ocr_and_uninterpreted_image_uncertainties_reach_primary_evidence():
    page=ParsedPage(page_number=1,text='Extracted formula text',source='ocr_fallback',blocks=[
        DocumentBlock(block_id='image1',type='image',page_number=1,source_anchor='synthetic.pdf, p. 1')])
    parsed=ParsedFile(filename='synthetic.pdf',file_type='.pdf',path='synthetic.pdf',pages=[page],raw_text=page.text)
    span=catalog_for([parsed])[0]
    assert any('OCR_NOT_HUMAN_VERIFIED' in item for item in span.extraction_warnings)
    assert any('UNINTERPRETED_VISUAL_CONTENT' in item for item in span.extraction_warnings)
    assert 'not calibrated' in span.confidence_basis
