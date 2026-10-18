"""Deterministic grading for a predeclared fixture experiment, not acceptance."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pointer(document, path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('source pointer must be absolute JSON pointer')
    value = document
    for token in path[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def grade(expected, supplied, input_document):
    if not isinstance(supplied, dict) or set(supplied) != {'answers'} or not isinstance(supplied['answers'], dict):
        raise ValueError('reader output requires only an answers object')
    answers = supplied['answers']
    rows = []
    for question_id, oracle in expected.items():
        row = {'question_id': question_id, 'answer_class': oracle['answer_class']}
        if question_id not in answers:
            row['result'] = 'unanswered'
        else:
            answer = answers[question_id]
            valid = isinstance(answer, dict) and set(answer) == {'value', 'sources'}
            sources = answer.get('sources') if valid else None
            supported = isinstance(sources, list) and bool(sources)
            if supported:
                for source in sources:
                    try:
                        pointer(input_document, source)
                        supported &= source in oracle['paths']
                    except (ValueError, KeyError, IndexError, TypeError):
                        supported = False
            same = valid and type(answer['value']) is type(oracle['value']) and answer['value'] == oracle['value']
            row['value_correct'] = same
            row['sources_valid'] = supported
            row['result'] = 'correct' if same and supported else 'incorrect'
            row['supplied'] = answer
        rows.append(row)
    return {'questions': len(expected), 'correct_known': sum(r['result']=='correct' and r['answer_class']=='known' for r in rows),
            'correct_unknown': sum(r['result']=='correct' and r['answer_class']=='unknown' for r in rows),
            'incorrect': sum(r['result']=='incorrect' for r in rows),
            'unanswered': sum(r['result']=='unanswered' for r in rows),
            'unsupported_extra_question_ids': sorted(set(answers) - set(expected)),
            'unsupported_extra_answers': [{'question_id':key,'result':'incorrect','supplied':answers[key]} for key in sorted(set(answers)-set(expected))],
            'total_incorrect_claims': sum(r['result']=='incorrect' for r in rows)+len(set(answers)-set(expected)), 'rows': rows}



def verify_freeze(freeze_path, expected_digest, manifest_path, source_root):
    if digest(freeze_path) != expected_digest:
        raise ValueError('independent frozen-protocol digest mismatch')
    frozen = json.loads(Path(freeze_path).read_text())
    if digest(manifest_path) != frozen['oracle_sha256']:
        raise ValueError('frozen oracle hash mismatch')
    for relative, expected in frozen['source_sha256'].items():
        if digest(Path(source_root) / relative) != expected:
            raise ValueError('frozen source/protocol hash mismatch')
    if digest(Path(manifest_path).parent / 'reader-prompt.txt') != frozen['prompt_sha256']:
        raise ValueError('frozen reader prompt hash mismatch')
    return frozen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('attempt_id')
    parser.add_argument('answer_file', type=Path)
    parser.add_argument('--freeze', type=Path, required=True)
    parser.add_argument('--expected-freeze-sha256', required=True)
    options = parser.parse_args()
    verify_freeze(options.freeze, options.expected_freeze_sha256, options.manifest, Path(__file__).parents[1])
    manifest = json.loads(options.manifest.read_text())
    case = next(c for c in manifest['cases'] if c['attempt_id'] == options.attempt_id)
    root = options.manifest.parent
    path = root / case['input_file']
    if digest(path) != case['input_sha256']:
        raise SystemExit('frozen input hash mismatch')
    document = json.loads(path.read_text())
    report = grade(case['oracle'], json.loads(options.answer_file.read_text()), document)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))


if __name__ == '__main__':
    main()
