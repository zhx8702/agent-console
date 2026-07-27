import type { ParticipationPreviewRequest } from "../../lib/api";

type ParsedClock = {
  year: number | null;
  month: number | null;
  day: number | null;
  hour: number;
  minute: number;
  second: number;
};

type ParsedHistoryLine = {
  speaker: string;
  content: string;
  clock: ParsedClock | null;
};

export type ParticipationHistoryExtraction = {
  draft: ParticipationPreviewRequest;
  matchedSignals: string[];
  caveats: string[];
};

const TIMESTAMP_PREFIX = /^\s*(?:\[\s*)?((?:\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+)?\d{1,2}:\d{2}(?::\d{2})?)(?:\s*\])?\s*/;
const BOT_NAME = /^(?:机器人|bot|小?助手|ai(?:助手)?)$/i;
const EXPLICIT_BOT_MENTION = /@\s*(?:机器人|bot|小?助手|ai(?:助手)?)(?=\s|[，,。.!！?？:：]|$)/i;
const BOT_ADDRESS = /(?:^|[\s，,])(?:机器人|bot|小助手|ai助手)[，,:：\s]/i;
const QUESTION = /[?？]|(?:吗|呢|么|怎么|为什么|为何|谁|哪(?:个|里)?|是不是|能否|可以吗)(?:[，,。.!！]?\s*)$/i;
const IDENTITY_QUESTION = /(?:你是谁|你的身份|你是(?:真人|人类|机器人|ai|人工智能)|是不是(?:真人|人类|机器人|ai)|谁在回复)/i;
const PRIVATE_MEMORY_INDUCEMENT = /(?=.*(?:私聊|私信|隐私|私密|私人|秘密|个人信息))(?=.*(?:记得|记住|回忆|调取|说出|告诉|透露|复述|重复|再说|再发|公开|发到群))/i;
const SENSITIVE_REPEAT = /(?=.*(?:身份证|手机号|电话号码|住址|银行卡|密码|病历|工资|收入|性取向|政治倾向|宗教信仰))(?=.*(?:重复|复述|再说|再发|发一遍|贴出来|公开|告诉))/i;
const CORRECTION = /(?:更正|纠正|你记错|刚才说错|不是.{0,24}(?:而是|是)|改成|应为)/i;
const TOPIC_CHANGE = /(?:换个话题|换一题|另一个问题|题外话|不聊这个|先不说这个|说点别的|新话题)/i;
const CONTINUATION = /(?:继续|接着|刚才|上面|前面|还是那个|回到刚才|更正|纠正)/i;
const UNFINISHED_TASK = /(?=.*(?:继续|接着|上次|刚才|还没|尚未|待办))(?=.*(?:任务|事情|问题|处理|完成|结果|进度))/i;
const MEMBER_ANSWER = /(?:答案是|正确答案|我来回答|我知道[，,:：]?|已经解决|问题解决了|不用机器人回答|结论是)/i;
const REPLY_TO_BOT = /(?:回复(?:机器人|bot|助手)|机器人刚才|助手刚才|你刚才|你记错)/i;
const EXPLICIT_COMMAND = /^(?:\/|!)(?:\S+)|(?:@\s*(?:机器人|bot|小?助手|ai(?:助手)?).{0,12}(?:帮|查|执行|设置|提醒|总结))/i;
const OTHER_MEMBER_MENTION = /@\s*(?!(?:机器人|bot|小?助手|ai(?:助手)?)(?:\s|[，,。.!！?？:：]|$))[^\s，,。.!！?？:：]{1,24}/i;

function parseClock(value: string): ParsedClock | null {
  const match = value.match(
    /^(?:(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?$/,
  );
  if (!match) return null;
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6] || 0);
  if (hour > 23 || minute > 59 || second > 59) return null;
  if (match[1]) {
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const check = new Date(Date.UTC(year, month - 1, day));
    if (
      check.getUTCFullYear() !== year
      || check.getUTCMonth() !== month - 1
      || check.getUTCDate() !== day
    ) {
      return null;
    }
  }
  return {
    year: match[1] ? Number(match[1]) : null,
    month: match[2] ? Number(match[2]) : null,
    day: match[3] ? Number(match[3]) : null,
    hour,
    minute,
    second,
  };
}

function parseHistory(history: string): ParsedHistoryLine[] {
  return history
    .normalize("NFKC")
    .split(/\r?\n/)
    .map((rawLine) => rawLine.trim())
    .filter(Boolean)
    .map((rawLine) => {
      const timestamp = rawLine.match(TIMESTAMP_PREFIX);
      const clock = timestamp ? parseClock(timestamp[1]) : null;
      const body = timestamp ? rawLine.slice(timestamp[0].length).trim() : rawLine;
      const speakerMatch = body.match(/^([^:：]{1,32})[:：]\s*(.*)$/);
      return {
        speaker: speakerMatch?.[1]?.trim() || "",
        content: (speakerMatch?.[2] ?? body).trim(),
        clock,
      };
    });
}

function isBotSpeaker(speaker: string) {
  return BOT_NAME.test(speaker.trim());
}

function clockSeconds(clock: ParsedClock) {
  return clock.hour * 3600 + clock.minute * 60 + clock.second;
}

function elapsedClockSeconds(start: ParsedClock, end: ParsedClock) {
  const startHasDate = start.year !== null;
  const endHasDate = end.year !== null;
  if (startHasDate !== endHasDate) return null;
  if (startHasDate && endHasDate) {
    const startEpoch = Date.UTC(
      start.year!,
      start.month! - 1,
      start.day!,
      start.hour,
      start.minute,
      start.second,
    ) / 1000;
    const endEpoch = Date.UTC(
      end.year!,
      end.month! - 1,
      end.day!,
      end.hour,
      end.minute,
      end.second,
    ) / 1000;
    return endEpoch - startEpoch;
  }
  const startSeconds = clockSeconds(start);
  let endSeconds = clockSeconds(end);
  if (endSeconds < startSeconds) endSeconds += 24 * 3600;
  return endSeconds - startSeconds;
}

function hasRapidMultiPartyWindow(lines: ParsedHistoryLine[]) {
  const timed = lines.slice(-40).filter(
    (line): line is ParsedHistoryLine & { clock: ParsedClock } => Boolean(line.clock),
  );
  for (let start = 0; start < timed.length; start += 1) {
    const speakers = new Set<string>();
    let count = 0;
    for (let end = start; end < timed.length; end += 1) {
      const elapsed = elapsedClockSeconds(timed[start].clock, timed[end].clock);
      if (elapsed === null || elapsed < 0 || elapsed > 15) break;
      count += 1;
      const speaker = timed[end].speaker.trim().toLocaleLowerCase();
      if (speaker && !isBotSpeaker(speaker)) speakers.add(speaker);
      if (count >= 4 && speakers.size >= 3) return true;
    }
  }
  return false;
}

function clockAsShanghaiIso(clock: ParsedClock) {
  const year = clock.year ?? 2020;
  const month = clock.month ?? 1;
  const day = clock.day ?? 2;
  // Natural WeChat snippets normally display the group-local wall clock.  The
  // simulator makes that deterministic by treating it as Asia/Shanghai; the
  // resulting structured ISO value remains editable before preview.
  return new Date(
    Date.UTC(
      year,
      month - 1,
      day,
      clock.hour - 8,
      clock.minute,
      clock.second,
    ),
  ).toISOString();
}

function addMatch(matches: string[], enabled: boolean, label: string) {
  if (enabled && !matches.includes(label)) matches.push(label);
}

/**
 * Convert an operator-provided snippet into the existing strict preview model.
 * The returned object contains signals and generic labels only: the raw snippet
 * is deliberately absent, so the controller cannot upload or persist it.
 */
export function extractParticipationSignals(
  history: string,
  current: ParticipationPreviewRequest,
): ParticipationHistoryExtraction {
  const lines = parseHistory(history);
  const last = lines[lines.length - 1];
  const lastContent = last?.content || "";
  // Treat the last non-empty line as the message being previewed.  Earlier
  // lines may describe its bounded chat window, but must never promote an old
  // address/safety signal into a hard obligation for an unrelated new message.
  const mentionedMe = EXPLICIT_BOT_MENTION.test(lastContent);
  const implicitlyAddressed = BOT_ADDRESS.test(lastContent);
  const identityQuestion = IDENTITY_QUESTION.test(lastContent);
  const explicitQuestion = Boolean(
    ((mentionedMe || implicitlyAddressed) && QUESTION.test(lastContent))
      || identityQuestion,
  );
  const privateMemoryInducement = PRIVATE_MEMORY_INDUCEMENT.test(lastContent);
  const sensitiveRepeat = SENSITIVE_REPEAT.test(lastContent);
  const correction = CORRECTION.test(lastContent);
  const topicChanged = TOPIC_CHANGE.test(lastContent);
  const topicContinuation = correction || CONTINUATION.test(lastContent);
  const unfinishedTaskContinuation = UNFINISHED_TASK.test(lastContent);
  const repliedToBot = REPLY_TO_BOT.test(lastContent);
  const explicitCommand = EXPLICIT_COMMAND.test(lastContent);
  const rapidMultiPartyChat = hasRapidMultiPartyWindow(lines);
  const memberAnswer = Boolean(
    last?.speaker
      && !isBotSpeaker(last.speaker)
      && MEMBER_ANSWER.test(lastContent),
  );
  const directedToOtherMember = OTHER_MEMBER_MENTION.test(lastContent);
  const safetyResponseRequired = privateMemoryInducement || sensitiveRepeat;
  const baseEligible = Boolean(
    mentionedMe
      || implicitlyAddressed
      || identityQuestion
      || repliedToBot
      || explicitCommand
      || safetyResponseRequired,
  );
  const lastClock = last?.clock || null;
  const botMessages = lines.slice(-40).filter((line) => isBotSpeaker(line.speaker)).length;
  const totalMessages = Math.min(lines.length, 40);
  const anyHeuristic = Boolean(
    baseEligible
      || rapidMultiPartyChat
      || memberAnswer
      || correction
      || topicChanged
      || topicContinuation
      || directedToOtherMember,
  );

  const draft: ParticipationPreviewRequest = {
    ...current,
    mentioned_me: mentionedMe,
    replied_to_bot: repliedToBot,
    explicit_command: explicitCommand,
    safety_response_required: safetyResponseRequired,
    explicit_question_to_bot: explicitQuestion,
    topic_continuation: topicContinuation,
    unfinished_task_continuation: unfinishedTaskContinuation,
    directed_to_other_member: directedToOtherMember,
    rapid_multi_party_chat: rapidMultiPartyChat,
    valid_member_answer_exists: memberAnswer,
    intent_confidence: anyHeuristic ? (baseEligible ? 0.9 : 0.7) : 0.5,
    base_eligible: baseEligible,
    base_reason: baseEligible ? "" : "not_addressed",
    bot_messages_last_40: botMessages,
    total_messages_last_40: totalMessages,
    topic_changed: topicChanged,
    reply_target_ambiguous: rapidMultiPartyChat && (mentionedMe || implicitlyAddressed),
    now: lastClock ? clockAsShanghaiIso(lastClock) : current.now,
  };

  const matchedSignals: string[] = [];
  addMatch(matchedSignals, mentionedMe, "明确 @ 机器人");
  addMatch(matchedSignals, rapidMultiPartyChat, "15 秒内多人快速对话");
  addMatch(matchedSignals, memberAnswer, "成员已给出答案的线索");
  addMatch(matchedSignals, identityQuestion, "机器人身份问题");
  addMatch(matchedSignals, privateMemoryInducement, "诱导披露私密记忆");
  addMatch(matchedSignals, sensitiveRepeat, "要求复述敏感信息");
  addMatch(matchedSignals, correction, "纠正或更正上下文");
  addMatch(matchedSignals, topicChanged, "话题已切换");
  addMatch(
    matchedSignals,
    Boolean(lastClock && (lastClock.hour >= 23 || lastClock.hour < 8)),
    "末条消息处于常见安静时段",
  );

  return {
    draft,
    matchedSignals,
    caveats: [
      "最后一条非空记录会被视为当前消息；更早记录只用于消息窗口线索。",
      "这是可复核的关键词与时间窗口规则，不是语义模型，可能漏判或误判。",
      "聊天片段只在当前浏览器页面解析；发送预览时只提交下方结构化信号。",
    ],
  };
}

export function createDefaultParticipationPreview(): ParticipationPreviewRequest {
  return {
    message_id: "preview",
    now: new Date().toISOString(),
    mentioned_me: false,
    replied_to_bot: false,
    explicit_command: false,
    safety_response_required: false,
    explicit_question_to_bot: false,
    keyword_triggered: false,
    topic_continuation: false,
    unfinished_task_continuation: false,
    directed_to_other_member: false,
    rapid_multi_party_chat: false,
    bot_replied_within_60s: false,
    valid_member_answer_exists: false,
    intent_confidence: 1,
    base_eligible: false,
    base_reason: "",
    bot_messages_last_40: 0,
    total_messages_last_40: 0,
    soft_replies_last_10m: 0,
    soft_replies_last_hour: 0,
    consecutive_bot_messages: 0,
    proactive_messages_today: 0,
    group_silence_seconds: 0,
    is_self_sent: false,
    topic_changed: false,
    superseded_by_newer_message: false,
    requested_proactive: false,
    response_kind: "short",
    reply_target_ambiguous: false,
  };
}
