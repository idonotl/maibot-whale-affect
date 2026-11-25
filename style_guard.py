"""Outbound style guidance. Never filters input, edits scores or suppresses replies."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

START, END = '【小鲸表达约束】', '【小鲸表达约束结束】'
PLANNER_ITEM_PREFIX = 'whale-affect-grounding-'
PLANNER_MARKER = '【小鲸事实与人称边界】'

FACT_BOUNDARIES = (
    '逐句按原始消息核对谁在做、谁承受、谁在感受。当前回复里的“我”是大肥鱼，“你”是当前回复对象；用户原话里的“我”属于用户，私聊中用户说的“你”通常指大肥鱼。',
    '第三方、引用、转述和视频内容保持原作者及原主语，不把其中的状态、动作或感受换给用户或大肥鱼；假设和试演不等于真实发生。',
    '对方明确追问、把你刚说的话复述成反问，或针对那句发问号、疑惑表情时，结合引用和紧邻的对话判断是否没听懂。即使只有表情或没有疑问词，也可以是在请你澄清。',
    '需要澄清时，先对准被引用或被复述的那句，再用一句白话解释。原句说反了或前后打架，就指出哪里说岔了并直接改口。上下文能看出原意时，只保留那个原意，把原句里多出来的矛盾条件删掉。只有一个表情且看不出在问哪句时，可以简短确认，不猜一个对象来解释。',
    '长时间没有消息只说明没有新消息，不能据此断定任何一方刚睡醒、关机、离开、重新上线或恢复；原话和可靠上下文没有依据就不写。',
)


def planner_prompt():
    return '\n'.join((PLANNER_MARKER, *FACT_BOUNDARIES,
        'reply_reference 只写有据可依的回应方向。对方在澄清时，写明实际被追问的那句。简短只删重复和客套，不要求残句、奇怪表达或牺牲句意。'))


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
        '普通打趣不是绕过禁句的许可，不向对方汇报禁词规则；引用发言和技术事实的归属保持准确。',
        '简短只删重复和客套，不删理解句意必需的主语、动作或停顿。允许自然口语、省略和倒装，但不要写奇怪表达、字词粘连、搭配残缺或歧义句；一句里有多个动作时，用正常标点或拆成两句。发送前默读一遍，读不顺就重写通顺。',
        '接梗前先想清这一句到底要表达什么。写完默读一遍，核对前后说的是同一个人和同一件事，条件、先后顺序和结果能接起来。',
        '若笑点要靠对方补一个没说出口的前提，或照字面读两半互相打架，就删掉那层转折，用一句平时会说的话表达原意。简单的邀请或打趣就直接说，俏皮话可以用想象和假设，现实里有什么、做过什么要有聊天依据。',
        *FACT_BOUNDARIES,
        'Planner 的回复方向只是参考，与原始消息或可靠上下文冲突时以原话为准，不沿用其中无依据的身份、状态或经历推断。', END])


def merge(existing, block):
    base = re.sub(re.escape(START)+r'.*?'+re.escape(END), '', str(existing), flags=re.S).strip()
    return (base+'\n\n'+block).strip()
