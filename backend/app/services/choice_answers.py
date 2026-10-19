"""Unambiguous option labels shared by generation validation and scoring."""
import re
import unicodedata


def answer_labels(value,options):
    text=unicodedata.normalize('NFKC',value).strip()
    normalized=lambda item:re.sub(r'\s+','',unicodedata.normalize('NFKC',item)).casefold()
    for index,option in enumerate(options):
        if normalized(text)==normalized(option):return {chr(65+index)}
    text=re.sub(r'^(?:答案|正确答案|answer)\s*[:：]\s*','',text,flags=re.I)
    text=text.strip('()[]（） ')
    if not re.fullmatch(r'[A-Ha-h](?:\s*[,、;\s]?\s*[A-Ha-h])*',text):return None
    labels=set(re.findall(r'[A-H]',text.upper()))
    available={chr(65+index) for index in range(len(options))}
    return labels if labels and labels.issubset(available) else None
