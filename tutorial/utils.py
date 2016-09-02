import lxml.etree as et
from snorkel.models import CandidateSet
from snorkel.candidates import TemporarySpan


def get_docs_xml(filepath, doc_path=".//document", id_path=".//id/text()"):
    xml = et.fromstring(open(filepath, 'rb').read())
    return dict(zip(xml.xpath(id_path), xml.xpath(doc_path)))


def get_CD_mentions_by_MESHID(doc_xml, sents):
    """
    Collect a set of Pubtator chemical-induced disease (CID) relation mention annotations.
    Returns a dictionary of (sent_id, char_start, char_end) tuples indexed by MESH ID.
    """
    sent_offsets = [s.abs_char_offsets[0] for s in sents]

    # Get unary mentions of diseases / chemicals
    unary_mentions = {}
    annotations = doc_xml.xpath('.//annotation')
    for a in annotations:

        # NOTE: Ignore CompositeRole individual mention annotations for now
        comp_roles = a.xpath('./infon[@key="CompositeRole"]/text()')
        comp_role = comp_roles[0] if len(comp_roles) > 0 else None
        if comp_role == 'IndividualMention':
            continue

        # Get basic annotation attributes
        txt = a.xpath('./text/text()')[0]
        offset = int(a.xpath('./location/@offset')[0])
        length = int(a.xpath('./location/@length')[0])
        type = a.xpath('./infon[@key="type"]/text()')[0]
        mesh = a.xpath('./infon[@key="MESH"]/text()')[0]
        
        # Get sentence id and relative character offset
        si = len(sent_offsets) - 1
        for i,so in enumerate(sent_offsets):
            if offset == so:
                si = i
                break
            elif offset < so:
                si = i - 1
                break
        sent       = sents[si]       
        char_start = offset - sent_offsets[si]
        char_end   = char_start + length - 1
        
        # Index by MESH ID as that is how relations refer to
        unary_mentions[mesh] = (sent, char_start, char_end, txt)
    return unary_mentions


def get_CID_relations(doc_xml, doc):
    """
    Given the doc XML and extracted unary mention tuples, return pairs of unary mentions that are annotated
    as CID relations.

    NOTE: This is somewhat ambiguous as relations are only marked at the entity level here...
    """
    unary_mentions = get_CD_mentions_by_MESHID(doc_xml, doc.sentences)
    annotations    = doc_xml.xpath('.//relation')
    for a in annotations:
        try:
            chemical = unary_mentions[a.xpath('./infon[@key="Chemical"]/text()')[0]]
            disease  = unary_mentions[a.xpath('./infon[@key="Disease"]/text()')[0]]
        except KeyError:
            continue

        # Only take relations in same sentence
        if chemical[0] == disease[0]:
            yield (chemical, disease)
