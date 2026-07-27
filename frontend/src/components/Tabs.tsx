import { type KeyboardEvent, type ReactNode, useId, useRef } from "react";

export type TabDefinition = {
  id: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
};

export type TabTriggerDefinition = Omit<TabDefinition, "content">;

type TabListProps = {
  tabs: TabTriggerDefinition[];
  activeId: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  idPrefix?: string;
  orientation?: "horizontal" | "vertical";
  className?: string;
  triggerClassName?: string | ((tab: TabTriggerDefinition, selected: boolean) => string);
};

type TabsProps = {
  tabs: TabDefinition[];
  activeId: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  orientation?: "horizontal" | "vertical";
  className?: string;
};

export function TabList({
  tabs,
  activeId,
  onChange,
  ariaLabel,
  idPrefix,
  orientation = "horizontal",
  className = "tabs-list",
  triggerClassName = "tabs-trigger",
}: TabListProps) {
  const generatedId = useId();
  const resolvedIdPrefix = idPrefix || generatedId;
  const buttonRefs = useRef(new Map<string, HTMLButtonElement>());
  const activeTab = tabs.find((tab) => tab.id === activeId && !tab.disabled) || tabs.find((tab) => !tab.disabled);

  const selectAndFocus = (tab: TabTriggerDefinition | undefined) => {
    if (!tab) return;
    onChange(tab.id);
    buttonRefs.current.get(tab.id)?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, tabId: string) => {
    const previousKey = orientation === "horizontal" ? "ArrowLeft" : "ArrowUp";
    const nextKey = orientation === "horizontal" ? "ArrowRight" : "ArrowDown";
    const enabledTabs = tabs.filter((tab) => !tab.disabled);
    if (!enabledTabs.length) return;
    if (event.key === previousKey || event.key === nextKey) {
      event.preventDefault();
      const currentIndex = enabledTabs.findIndex((tab) => tab.id === tabId);
      const direction = event.key === nextKey ? 1 : -1;
      selectAndFocus(enabledTabs[(Math.max(currentIndex, 0) + direction + enabledTabs.length) % enabledTabs.length]);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      selectAndFocus(event.key === "Home" ? enabledTabs[0] : enabledTabs[enabledTabs.length - 1]);
    }
  };

  if (!activeTab) return null;

  return (
    <div className={className} role="tablist" aria-label={ariaLabel} aria-orientation={orientation}>
      {tabs.map((tab) => {
        const selected = tab.id === activeTab.id;
        const resolvedTriggerClass = typeof triggerClassName === "function"
          ? triggerClassName(tab, selected)
          : triggerClassName;
        return (
          <button
            key={tab.id}
            ref={(element) => {
              if (element) buttonRefs.current.set(tab.id, element);
              else buttonRefs.current.delete(tab.id);
            }}
            type="button"
            id={`${resolvedIdPrefix}-tab-${tab.id}`}
            className={resolvedTriggerClass}
            role="tab"
            aria-selected={selected}
            aria-controls={`${resolvedIdPrefix}-panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => handleKeyDown(event, tab.id)}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

export function Tabs({
  tabs,
  activeId,
  onChange,
  ariaLabel,
  orientation = "horizontal",
  className = "",
}: TabsProps) {
  const generatedId = useId();
  const activeTab = tabs.find((tab) => tab.id === activeId && !tab.disabled) || tabs.find((tab) => !tab.disabled);

  if (!activeTab) return null;

  return (
    <div className={`tabs${className ? ` ${className}` : ""}`}>
      <TabList
        tabs={tabs}
        activeId={activeTab.id}
        onChange={onChange}
        ariaLabel={ariaLabel}
        idPrefix={generatedId}
        orientation={orientation}
      />
      <div
        id={`${generatedId}-panel-${activeTab.id}`}
        className="tabs-panel"
        role="tabpanel"
        aria-labelledby={`${generatedId}-tab-${activeTab.id}`}
        tabIndex={0}
      >
        {activeTab.content}
      </div>
    </div>
  );
}
