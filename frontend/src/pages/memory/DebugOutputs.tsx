import { OutputPanel } from "../../components/OutputPanel";

type DebugOutputsProps = {
  meta: string;
  identity: string;
  session: string;
  memoryItems: string;
  profileEnrichment: string;
  extractionJobs: string;
  memoryGraph: string;
  runtime: string;
};

export function DebugOutputs({
  meta,
  identity,
  session,
  memoryItems,
  profileEnrichment,
  extractionJobs,
  memoryGraph,
  runtime,
}: DebugOutputsProps) {
  return (
    <>
      <OutputPanel title="群会话 / 成员响应" value={meta} />
      <OutputPanel title="全局身份记忆响应" value={identity} />
      <OutputPanel title="会话记忆响应" value={session} />
      <div className="admin-notice admin-notice-warning">
        技术详情：用于排障时查看状态、计数、ID 和字段名；不提供聊天正文导出。
      </div>
      <OutputPanel title="单条记忆原始元数据" value={memoryItems} />
      <OutputPanel title="画像候选原始元数据" value={profileEnrichment} />
      <OutputPanel title="抽取任务原始元数据" value={extractionJobs} />
      <OutputPanel title="记忆图谱原始元数据" value={memoryGraph} />
      <OutputPanel title="运行时 / 回填原始元数据" value={runtime} />
    </>
  );
}
