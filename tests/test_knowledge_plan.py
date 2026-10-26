"""Full-source planning and per-cell evidence boundaries, without paid providers."""
import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.schemas.document import ExtractionResult
from app.schemas.rag import PrepareIndexRequest
from app.services.chunking import chunk_document
from app.services.content_map import content_map
from app.services.grounded_generation import GroundedGeneration
from app.services.question_generator import QuestionGenerator
from app.services.retrieval import RagService
from tests.rag_http_fixture import Repository, Embedding
from tests.test_rag import command


def extracted(texts):
    return ExtractionResult(contract_version='document-extraction.v1',source_type='PDF',source_sha256='a'*64,parser_version='fixture',include_notes=False,status='EXTRACTED',unit_count=len(texts),spans=[{'kind':'PAGE','position':i+1,'text':text} for i,text in enumerate(texts)],warnings=[])


def outline(texts):
    req=command()
    chunks=[{**chunk.model_dump(),'document_id':req.scope.document_id,'version_id':req.scope.version_id,'source_sha256':'a'*64} for chunk in chunk_document(req.index_id,extracted(texts))]
    return content_map(req.index_id,'Data structures',chunks)


def test_outline_reads_all_units_and_unbounded_numbered_hierarchy():
    texts=['Chapter 1: Structures\nStack uses LIFO.']+[f"{'.'.join(['1']*depth)} Nested level {depth}\nStack example." for depth in range(2,22)]
    result=outline(texts)
    assert len(result.source_units)==21
    assert len(result.nodes)==21
    assert sum(node.parent_id is None for node in result.nodes)==1
    assert result.nodes[-1].source_units[0].position==21
    assert all(node.origin=='HEADING' for node in result.nodes)


def test_missing_outline_proposes_reviewable_page_topics_including_late_pages():
    result=outline([f'Topic page {i}\nExplain concept {i}.' for i in range(1,13)])
    assert len(result.source_units)==12
    assert 'INFERRED_TOPICS' in result.warnings
    assert any(node.name=='Topic page 12' for node in result.nodes)
    assert all(node.origin=='INFERRED' for node in result.nodes)


@pytest.mark.parametrize("title", ['Phần cứng máy tính', 'Partition tables', 'Chapter iv: Topic', 'Chapter IVORY', 'Chapter 12abc'])
def test_non_headings_do_not_match_chapter_prefixes(title):
    result = outline([title + '\nExplanation of the topic.'])
    assert all(node.origin == 'INFERRED' for node in result.nodes)


@pytest.mark.parametrize("title", ['pHầN IV: Chủ đề', 'CHAPTER 12: Topic', 'part M: Topic'])
def test_chapter_keywords_are_case_insensitive_but_codes_are_not(title):
    result = outline([title + '\nExplanation of the topic.'])
    assert any(node.origin == 'HEADING' for node in result.nodes)


def test_multiline_chapter_titles_and_repeated_page_headers():
    result=outline(['CHƯƠNG 1\nCấu trúc dữ liệu\nNội dung chương.'])
    assert result.nodes[0].name=='Cấu trúc dữ liệu'
    result=outline(['University course\nStack ordering','University course\nQueue ordering','University course\nTree traversal'])
    assert {node.name for node in result.nodes if node.parent_id}=={'Stack ordering','Queue ordering','Tree traversal'}


def test_numbered_slide_continuations_merge_sources_and_keep_first_evidence():
    result=outline([
        'Chapter 3: Object creation\n3.2. Constructor method\nInitialize fields.',
        '3.2. Constructor method(2)\nConstructor parameters.',
        '3.2. Constructor method (3)\nAccess control.',
        '3.2. Constructor method (4)\nDefault constructor.',
        '3.3. Object declaration and initialization(2)\nObject references.',
        '3.3. Object declaration and initialization (3)\nArrays of objects.',
    ])
    constructors=[n for n in result.nodes if n.name=='Constructor method']
    assert len(constructors)==1
    assert [u.position for u in constructors[0].source_units]==[1,2,3,4]
    assert constructors[0].evidence=='3.2. Constructor method'
    objects=[n for n in result.nodes if n.name=='Object declaration and initialization']
    assert len(objects)==1 and [u.position for u in objects[0].source_units]==[5,6]
    assert len(result.nodes)==3
    assert {u.position for u in result.nodes[0].source_units}==set(range(1,7))


def test_continuation_can_shorten_same_section_title_without_merging_other_sections():
    result=outline([
        '1.1. Abstraction\nDefinition.',
        '1.2. Abstraction in OOP\nObjects in reality.',
        '1.2. Abstraction (3)\nEssential properties.',
        '1.2. Abstraction (4)\nClasses represent entities.',
        '1.3. Abstraction (2)\nA different section.',
    ])
    oop=[n for n in result.nodes if n.name=='Abstraction in OOP']
    assert len(oop)==1 and [u.position for u in oop[0].source_units]==[2,3,4]
    assert len(result.nodes)==3
    assert len({n.id for n in result.nodes})==3


def test_inferred_continuations_normalize_case_spacing_unicode_and_keep_notes():
    req=command()
    document=extracted(['Constructor method\nInitialize fields.',
                        'Constructor   method(2)\nConstructor parameters.',
                        'CONSTRUCTOR METHOD （ 3 ）\nDefault constructor.'])
    document.include_notes=True
    document.spans.append(type(document.spans[0])(kind='NOTES',position=3,text='Constructor method (4)\nSpeaker explanation.'))
    chunks=[{**c.model_dump(),'document_id':req.scope.document_id,'version_id':req.scope.version_id,'source_sha256':'a'*64} for c in chunk_document(req.index_id,document)]
    first=content_map(req.index_id,'OOP',chunks)
    assert first==content_map(req.index_id,'OOP',list(reversed(chunks)))
    assert len(first.nodes)==2
    child=next(n for n in first.nodes if n.parent_id)
    assert child.name=='Constructor method'
    assert {(u.kind,u.position) for u in child.source_units}=={('PAGE',1),('PAGE',2),('PAGE',3),('NOTES',3)}


def test_same_titles_under_distinct_chapters_and_named_qualifiers_stay_separate():
    result=outline([
        'Chapter 1: Java\n1.1. Constructor method\nJava example.',
        '1.1. Constructor method (2)\nJava continuation.',
        'Chapter 2: C++\n2.1. Constructor method\nC++ example.',
        '2.1. Constructor method (2)\nC++ continuation.',
        '2.2. Constructor method (Java)\nNamed qualifier.',
        '2.3. Complexity O(n)\nComplexity notation.',
    ])
    constructors=[n for n in result.nodes if n.name=='Constructor method']
    assert len(constructors)==2 and constructors[0].parent_id!=constructors[1].parent_id
    assert [u.position for u in constructors[0].source_units]==[1,2]
    assert [u.position for u in constructors[1].source_units]==[3,4]
    assert any(n.name=='Constructor method (Java)' for n in result.nodes)
    assert any(n.name=='Complexity O(n)' for n in result.nodes)


def test_outline_endpoint_returns_merged_topic_with_all_source_units():
    req=command()
    document=extracted(['2.1. Encapsulation\nInternal and external views.',
                        '2.1. Encapsulation (2)\nAttributes and methods.'])
    chunks=[{**c.model_dump(),'document_id':req.scope.document_id,'version_id':req.scope.version_id,'source_sha256':'a'*64} for c in chunk_document(req.index_id,document)]
    repository=AsyncMock()
    repository.outline_chunks.return_value=chunks
    with TestClient(create_app(Settings(internal_api_token='outline-fixture'))) as client:
        client.app.state.rag=RagService(repository,Embedding())
        response=client.post('/internal/rag/outline',headers={'X-Internal-Token':'outline-fixture'},json={
            'owner_id':str(req.scope.owner_id),'index_id':str(req.index_id),
            'profile_id':req.profile_id,'title':'OOP'})
        assert response.status_code==200,response.text
        nodes=response.json()['nodes']
        assert len(nodes)==1 and nodes[0]['name']=='Encapsulation'
        assert nodes[0]['source_units']==[{'kind':'PAGE','position':1},{'kind':'PAGE','position':2}]
        repository.outline_chunks.assert_awaited_once_with(req.scope.owner_id,req.profile_id,req.index_id)


class Provider:
    def __init__(self):self.calls=0;self.borrow=False;self.callback=None;self.wrong_answers=False
    async def generate_grounded(self,prompt):
        self.calls+=1
        slots=json.loads(prompt.split('Slots:\n',1)[1]);questions=[]
        for i,slot in enumerate(slots):
            chunk=slots[0 if self.borrow else i]['excerpts'][0]
            assert slot['index']==i+1 and slot['option_count']==4
            correct=slot['correct_option_count']
            assert correct== (1 if slot['question_type']=='SINGLE_CHOICE' else 2)
            assert correct+slot['incorrect_option_count']==4
            questions.append({'question_text':f'Question {i}: {slot["knowledge"]}?','options':[{'text':str(j),'isCorrect':j<(1 if self.wrong_answers else correct)} for j in range(4)],'citations':[{'chunk_id':chunk['chunk_id'],'quote':chunk['text']}]})
        if self.callback:await self.callback()
        return json.dumps({'questions':questions})
    async def generate(self,prompt):raise AssertionError('No text fallback')
    async def aclose(self):pass


@pytest.fixture
def planned():
    repo=Repository();rag=RagService(repo,Embedding());provider=Provider();req=command();headers={'X-Internal-Token':'knowledge-plan-test'}
    with TestClient(create_app(Settings(internal_api_token='knowledge-plan-test'))) as client:
        client.app.state.rag=rag;client.app.state.grounded_generator=GroundedGeneration(rag,QuestionGenerator(provider))
        source=PrepareIndexRequest(**req.model_dump(),extraction=extracted(['Stack uses LIFO.','Queue uses FIFO.']))
        assert client.post('/internal/rag/prepare',headers=headers,json=source.model_dump(mode='json')).status_code==200
        assert client.post('/internal/rag/step',headers=headers,json=req.model_dump(mode='json')).status_code==200
        configs=[{'question_type':'SINGLE_CHOICE','question_format':'KNOWLEDGE','count':1}]*2
        body={'owner_id':str(req.scope.owner_id),'profile_id':req.profile_id,'index_ids':[str(req.index_id)],'query':'Data structures','top_k':8,'question_configs':configs,'plan_items':[{**config,'id':f'cell-{i}','knowledge_title':title,'objective':f'Explain {title}','source_units':[{'kind':'PAGE','position':i+1}]} for i,(config,title) in enumerate(zip(configs,['Stack','Queue']))]}
        yield client,provider,repo,req,body,headers


def test_each_cell_uses_own_sources_and_server_assigns_plan_ids(planned):
    client,provider,_,_,body,headers=planned
    result=client.post('/internal/rag/generate',headers=headers,json=body)
    assert result.status_code==200,result.text
    questions=result.json()['questions']
    assert [q['plan_item_id'] for q in questions]==['cell-0','cell-1']
    assert [q['citations'][0]['chunk']['position'] for q in questions]==[1,2]
    assert all(q['correct_answer'] is None for q in questions)
    assert provider.calls==1


@pytest.mark.parametrize('wrong_answers',[False,True])
def test_planned_mixed_answers_have_explicit_budgets_and_safe_diagnostics(planned,caplog,wrong_answers):
    client,provider,_,_,body,headers=planned
    body['question_configs']=[dict(c) for c in body['question_configs']]
    body['question_configs'][1]['question_type']='MULTIPLE_CHOICE'
    body['plan_items'][1]['question_type']='MULTIPLE_CHOICE'
    provider.wrong_answers=wrong_answers
    response=client.post('/internal/rag/generate',headers=headers,json=body)
    assert provider.calls==1
    if wrong_answers:
        assert response.status_code==502
        assert response.json()['detail']['code']=='RAG_OUTPUT_INVALID'
        assert 'reason=answers slot=2 question_type=MULTIPLE_CHOICE correct_count=1 option_count=4' in caplog.text
        assert 'Stack' not in caplog.text and 'Queue' not in caplog.text
    else:
        assert response.status_code==200,response.text
        assert [sum(o['isCorrect'] for o in q['options']) for q in response.json()['questions']]==[1,2]


def test_cannot_borrow_another_cells_evidence(planned):
    client,provider,_,_,body,headers=planned;provider.borrow=True
    response=client.post('/internal/rag/generate',headers=headers,json=body)
    assert response.status_code==502
    assert provider.calls==1


@pytest.mark.parametrize('failure,reason', [
    ('json', 'schema'),
    ('schema', 'schema'),
    ('duplicate_invalid_quote', 'quote_mismatch'),
    ('unknown', 'unknown_chunk'),
    ('quote', 'quote_mismatch'),
])
def test_planned_invalid_output_logs_reason_without_source_or_provider_data(planned, monkeypatch, caplog, failure, reason):
    client,provider,_,_,body,headers=planned
    original=provider.generate_grounded
    async def invalid_output(prompt):
        result=json.loads(await original(prompt))
        if failure=='json':
            return 'private-provider-output: invalid JSON'
        question=result['questions'][0]
        if failure=='schema':
            question['question_text']={'private-provider-output':'not a string'}
        elif failure=='duplicate_invalid_quote':
            question['citations'].append({**question['citations'][0], 'quote':'private-provider-output: unsupported quote'})
        elif failure=='unknown':
            question['citations'][0]['chunk_id']=str(uuid4())
        elif failure=='quote':
            question['citations'][0]['quote']='private-provider-output: unsupported quote'
        return json.dumps(result)
    monkeypatch.setattr(provider,'generate_grounded',invalid_output)
    response=client.post('/internal/rag/generate',headers=headers,json=body)
    assert response.status_code==502
    assert response.json()['detail']=={'code':'RAG_OUTPUT_INVALID','retryable':False}
    assert provider.calls==1
    assert f'generation_invalid_output reason={reason} mode=planned' in caplog.text
    if reason!='schema':
        assert 'slot=1' in caplog.text
    for private in ['Stack','Queue','private-provider-output',headers['X-Internal-Token']]:
        assert private not in caplog.text and private not in response.text


@pytest.mark.parametrize('split_quotes',[False,True])
def test_planned_repeated_chunk_references_keep_all_verified_evidence(planned, monkeypatch, split_quotes):
    client,provider,_,_,body,headers=planned
    original=provider.generate_grounded
    async def repeated(prompt):
        value=json.loads(await original(prompt))
        for question in value['questions']:
            citation=question['citations'][0]
            question['citations']=[dict(citation),dict(citation)]
            if split_quotes:
                question['citations'][0]['quote']=citation['quote'].split()[0]
                question['citations'][1]['quote']=citation['quote'].split()[-1]
        return json.dumps(value)
    monkeypatch.setattr(provider,'generate_grounded',repeated)
    response=client.post('/internal/rag/generate',headers=headers,json=body)
    assert response.status_code==200,response.text
    questions=response.json()['questions']
    assert [q['citations'][0]['quote'] for q in questions]==['Stack uses LIFO.','Queue uses FIFO.']
    assert all(len(q['citations'])==1 for q in questions)
    assert [q['plan_item_id'] for q in questions]==['cell-0','cell-1']
    assert provider.calls==1


def test_missing_cell_never_falls_back_and_foreign_scope_never_calls_model(planned):
    client,provider,_,_,body,headers=planned
    body['plan_items'][1]['source_units'][0]['position']=99
    response=client.post('/internal/rag/generate',headers=headers,json=body)
    assert response.status_code==422,response.text
    assert provider.calls==0
    body['owner_id']=str(uuid4())
    assert client.post('/internal/rag/generate',headers=headers,json=body).status_code==404
    assert provider.calls==0


def test_plan_rechecks_revocation_after_paid_call(planned):
    client,provider,repo,req,body,headers=planned
    async def revoke():await repo.revoke(req)
    provider.callback=revoke
    assert client.post('/internal/rag/generate',headers=headers,json=body).status_code==404
    assert provider.calls==1
