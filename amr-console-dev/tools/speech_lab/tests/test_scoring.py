"""Speech Lab measurements: text normalisation, word alignment, error rates, key words, signal."""
import importlib.util
import io
import math
from pathlib import Path
import struct
import wave

import pytest

_spec = importlib.util.spec_from_file_location('speech_lab_server', Path(__file__).resolve().parents[1] / 'server.py')
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)


def wav(samples, rate=16000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return out.getvalue()


# -- normal -----------------------------------------------------------------------------------

def test_normal_folds_case_and_turns_punctuation_into_spaces():
    assert lab.normal("Give the FILE to Divya's  desk!") == 'give the file to divya s desk'
    assert lab.normal('file-ஐ Divya-கிட்ட') == 'file ஐ divya கிட்ட'
    assert lab.normal("three o'clock-க்கு") == 'three o clock க்கு'


def test_normal_keeps_tamil_vowel_signs_and_composes_unicode():
    # Vowel signs are marks, not punctuation: they must survive.
    assert lab.normal('கொடுங்க') == 'கொடுங்க'
    assert lab.normal('café') == 'café'          # NFC
    assert lab.normal('') == ''


# -- align and distance -------------------------------------------------------------------------

def test_align_marks_substitutions_deletions_and_insertions():
    assert lab.align(['a', 'b', 'c'], ['a', 'b', 'c']) == [('ok', 'a', 'a'), ('ok', 'b', 'b'), ('ok', 'c', 'c')]
    assert lab.align(['a', 'b', 'c'], ['a', 'x', 'c']) == [('ok', 'a', 'a'), ('sub', 'b', 'x'), ('ok', 'c', 'c')]
    assert lab.align(['a', 'b', 'c'], ['a', 'c']) == [('ok', 'a', 'a'), ('del', 'b', ''), ('ok', 'c', 'c')]
    assert lab.align(['a', 'c'], ['a', 'b', 'c']) == [('ok', 'a', 'a'), ('ins', '', 'b'), ('ok', 'c', 'c')]
    assert lab.align([], ['a']) == [('ins', '', 'a')]
    assert lab.align(['a'], []) == [('del', 'a', '')]


def test_align_cost_equals_edit_distance():
    pairs = [('give the file to divya', 'give a file divya now'), ('', 'one two'), ('one two', ''),
             ('a b c d', 'd c b a')]
    for reference, heard in pairs:
        steps = lab.align(reference.split(), heard.split())
        assert sum(kind != 'ok' for kind, _, _ in steps) == lab.distance(reference.split(), heard.split())


def test_distance_is_levenshtein():
    assert lab.distance('kitten', 'sitting') == 3
    assert lab.distance('', 'abc') == 3
    assert lab.distance('abc', 'abc') == 0
    assert lab.distance('flaw', 'lawn') == 2


# -- key words ---------------------------------------------------------------------------------

@pytest.mark.parametrize('keyword,heard,found', [
    ('ready', 'is the encoder ready', True),
    ('ready', 'is the encoder already done', False),      # not inside another word
    ('divya', 'திவ்யா divyas desk', True),                # suffixes count
    ('திவ்யா', 'இந்த கோப்பை திவ்யாவிடம் கொடுங்க', True),
    ('கொடு', 'இந்த கோப்பை திவ்யாவிடம் கொடுங்க', True),
    ('3', 'moved to 3 pm', True),
    ('3', 'moved to 3pm', True),
    ('3', 'moved to 13 pm', False),
    ('3', 'moved to 30 minutes', False),
    ('3', 'room 30 then 3', True),                         # a later word can still match
    ('three pm', 'the meeting moved to three pm', True),
    ('file', '', False),
])
def test_keyword_found(keyword, heard, found):
    assert lab.keyword_found(keyword, heard) is found


# -- score -------------------------------------------------------------------------------------

def test_score_of_a_perfect_take():
    result = lab.score('Give the file to Divya.', 'give the file to Divya', 'file;Divya')
    assert (result['words'], result['word_errors'], result['wer'], result['cer']) == (5, 0, 0.0, 0.0)
    assert result['keywords'] == [{'word': 'file', 'found': True}, {'word': 'divya', 'found': True}]
    assert (result['keywords_found'], result['keywords_total']) == (2, 2)


def test_score_counts_word_and_character_errors():
    result = lab.score('Give the file to Divya.', 'give a file Divya', '')
    # 'the' -> 'a' (substitution), 'to' deleted: 2 of 5 words.
    assert (result['word_errors'], result['wer']) == (2, 0.4)
    # Characters without spaces: 'givethefiletodivya' (18) against 'giveafiledivya'.
    assert result['cer'] == round(lab.distance('givethefiletodivya', 'giveafiledivya') / 18, 3)
    assert result['keywords_total'] == 0


def test_score_keyword_spellings_and_false_matches():
    result = lab.score('Ask Arun whether the encoder is ready.', 'ask arun whether the encoder is already',
                       'Arun;encoder;ready')
    assert [k['found'] for k in result['keywords']] == [True, True, False]
    result = lab.score('Tell Meera the meeting moved to three pm.', 'tell meera the meeting moved to 13 pm',
                       'Meera;meeting;three|3')
    assert [k['found'] for k in result['keywords']] == [True, True, False]
    result = lab.score('Tell Meera the meeting moved to three pm.', 'tell meera the meeting moved to 3 pm',
                       'Meera;meeting;three|3')
    assert result['keywords_found'] == 3
    # Empty groups and spellings made only of punctuation are ignored.
    assert lab.score('a', 'a', ';;|.;a')['keywords_total'] == 1


def test_every_sentence_in_the_lab_list_scores_its_own_text_as_perfect():
    rows = lab.load_sentences()
    assert len(rows) >= 12
    for row in rows:
        result = lab.score(row['text'], row['text'], row['keywords'])
        assert result['wer'] == 0 and result['cer'] == 0, row['key']
        assert result['keywords_found'] == result['keywords_total'] > 0, (row['key'], result['keywords'])


def test_score_of_an_empty_reference_counts_insertions():
    result = lab.score('', 'hello there', '')
    assert (result['words'], result['word_errors']) == (1, 2)


# -- signal ------------------------------------------------------------------------------------

def test_signal_of_silence_and_of_a_tone():
    quiet = lab.signal(wav([0] * 16000))
    assert quiet['peak_db'] == -120.0 and quiet['clipped_pct'] == 0 and quiet['snr_db'] is None
    tone = [int(16384 * math.sin(2 * math.pi * 440 * n / 16000)) for n in range(16000)]
    loud = lab.signal(wav(tone))
    assert -6.5 < loud['peak_db'] < -5.5            # half of full scale is -6 dB
    assert -9.6 < loud['rms_db'] < -8.5             # a sine's RMS is 3 dB under its peak
    assert loud['clipped_pct'] == 0


def test_signal_reports_clipping_and_noise_floor():
    samples = [32767 if n % 2 else -32768 for n in range(8000)] + [100 if n % 2 else -100 for n in range(8000)]
    result = lab.signal(wav(samples))
    assert result['clipped_pct'] == 50.0
    assert result['snr_db'] is not None and result['snr_db'] > 40
