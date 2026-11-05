"""Outbound style guidance. Never filters input, edits scores or suppresses replies."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

START, END = '【小鲸表达约束】', '【小鲸表达约束结束】'


def load_rules():
    data = json.loads(Path(__file__).with_name('style_rules.json').read_text(encoding='utf-8'))
    if data.get('version') != 1:
        raise ValueError('unsupported_style_rules')
    return data


def normalize(text):
    return re.sub(r'[\s\u200b-\u200d\ufeff]', '', unicodedata.normalize('NFKC', str(text)))


def exemption(text):
    """Only current target text can grant a one-turn exception; mentioning is insufficient."""
    raw = str(text).strip()
    clauses = re.split(r'[。！？!?；;\n]', raw)
    clauses = [c for c in clauses if not re.search(r'别|不要|不许|禁止|不能|不准|不想|不用|不必|无需|不得|避免|拒绝', c)]
    positive = '。'.join(clauses)
    all_allowed = bool(re.search(r'(?:来|说|写|模仿|表演).{0,12}(?:一段|一句|一下)?.{0,6}鱼八股', positive)
                       or re.search(r'(?:展示|列出|解释|看看|给我看).{0,10}禁词(?:表|清单)', positive))
    quoting = bool(re.search(r'解释|什么意思|啥意思|引用|复述|讨论|原话|(?:说|念|读|重复).{0,4}(?:一句|这句话|这几个字)', positive))
    return all_allowed, normalize(positive) if quoting else ''


def find_hits(response, target_text, rules):
    if not rules.get('enabled'):
        return []
    allow_all, quoted = exemption(target_text)
    if allow_all:
        return []
    text = normalize(response)
    return [(group['id'], phrase) for group in rules['groups'] for phrase in group['phrases']
            if normalize(phrase) in text and normalize(phrase) not in quoted]


def prompt(rules, target_text=''):
    if not rules.get('enabled'):
        return ''
    allow_all, quoted = exemption(target_text)
    if allow_all:
        instruction = '对方这轮明确要求引用、讨论禁词表或模仿鱼八股，可以按请求使用；仅当前回复有效，不改变后续风格。'
        if '禁词' in target_text:
            instruction += '当前实际清单：' + '、'.join(p for g in rules['groups'] for p in g['phrases']) + '。不要虚构清单外的禁令。'
    else:
        phrases = [p for g in rules['groups'] for p in g['phrases'] if normalize(p) not in quoted]
        instruction = '日常回复不使用这些完整套话及明显变体：' + '、'.join(phrases) + '。'
        if quoted:
            instruction += '可引用解释对方本轮明确询问的原词，不扩展为日常口癖。'
    return '\n'.join([START, instruction,
        '保留想说的具体内容和自己的反应，不统一换成固定安慰。心疼、抱抱、我在、我觉得、我选、正常认同都可以。',
        '普通闲聊不用闭环、赋能、颗粒度、底层逻辑包装关心，不擅自用“你不是…你只是…”诊断对方，不给小事强行升华；真正讨论概念、正常澄清或按要求分析时可以正常说。',
        '普通打趣不是绕过禁句的许可，不向对方汇报禁词规则；引用发言和技术事实的归属保持准确。', END])


def merge(existing, block):
    base = re.sub(re.escape(START)+r'.*?'+re.escape(END), '', str(existing), flags=re.S).strip()
    return (base+'\n\n'+block).strip()
