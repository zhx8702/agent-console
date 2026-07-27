import { useEffect, useState } from "react";

import { OutputPanel } from "../components/OutputPanel";
import { useConsoleConfig } from "../state/console-config";
import { DocumentsWorkspace } from "./knowledge/DocumentsWorkspace";
import { FaqWorkspace } from "./knowledge/FaqWorkspace";
import { ImportWorkspace } from "./knowledge/ImportWorkspace";
import { KnowledgeHeader } from "./knowledge/KnowledgeHeader";
import { useDocumentsController } from "./knowledge/useDocumentsController";
import { useFaqController } from "./knowledge/useFaqController";
import { useImportController } from "./knowledge/useImportController";
import { useKnowledgeScope } from "./knowledge/useKnowledgeScope";

export function KnowledgePage() {
  const {
    config,
    verifiedGroupIds,
    registerVerifiedGroups,
    selectVerifiedGroup,
  } = useConsoleConfig();
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const scope = useKnowledgeScope({
    config,
    verifiedGroupIds,
    registerVerifiedGroups,
    selectVerifiedGroup,
  });
  const faq = useFaqController({ config, effectiveSessionId: scope.effectiveSessionId, setOutput });
  const documents = useDocumentsController({
    config,
    effectiveSessionId: scope.effectiveSessionId,
    setOutput,
  });
  const importer = useImportController({
    config,
    effectiveSessionId: scope.effectiveSessionId,
    sessions: scope.sessions,
    selectedFaqId: faq.selectedId,
    loadFaqs: faq.load,
    setOutput,
  });

  useEffect(() => {
    if (!scope.isScopeReady) {
      faq.setPreview(null);
      documents.setSearchResult(null);
      return;
    }
    void faq.load(null);
    void documents.load(null);
    faq.setPreview(null);
    documents.setSearchResult(null);
  }, [scope.effectiveSessionId, scope.isScopeReady, faq.load, documents.load, faq.setPreview, documents.setSearchResult]);

  return (
    <div className="page-grid">
      <KnowledgeHeader scope={scope} faqCount={faq.items.length} documentCount={documents.items.length} />
      {!scope.isScopeReady ? (
        <section className="panel span-3">
          <div className="admin-notice">请从已认证的群聊 roster 中选择目标群；搜索文本不会被当作 session_id。</div>
        </section>
      ) : scope.activeTab === "faq" ? (
        <FaqWorkspace faq={faq} currentScopeText={scope.currentScopeText} />
      ) : scope.activeTab === "docs" ? (
        <DocumentsWorkspace documents={documents} />
      ) : (
        <ImportWorkspace importer={importer} currentScopeText={scope.currentScopeText} />
      )}
      <OutputPanel title="知识运营响应" value={output} />
    </div>
  );
}
