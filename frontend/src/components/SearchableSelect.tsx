import { useEffect, useId, useMemo, useRef, useState } from "react";

export type SearchableSelectOption = {
  value: string;
  label: string;
  keywords?: string[];
};

type SearchableSelectProps = {
  value: string;
  options: SearchableSelectOption[];
  onChange: (value: string) => void;
  placeholder: string;
  searchPlaceholder?: string;
  emptyText?: string;
  noResultsText?: string;
  disabled?: boolean;
};

function normalizeKeyword(value: string) {
  return value.trim().toLowerCase();
}

export function SearchableSelect({
  value,
  options,
  onChange,
  placeholder,
  searchPlaceholder = "输入关键词筛选",
  emptyText = "暂无可选项",
  noResultsText = "没有匹配结果",
  disabled = false,
}: SearchableSelectProps) {
  const generatedId = useId();
  const listboxId = `${generatedId}-listbox`;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const comboboxRef = useRef<HTMLInputElement | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);

  const selectedOption = useMemo(
    () => options.find((item) => item.value === value) || null,
    [options, value],
  );

  const filteredOptions = useMemo(() => {
    const normalizedQuery = normalizeKeyword(query);
    if (!normalizedQuery) {
      return options;
    }
    return options.filter((item) => {
      const haystack = [item.label, item.value, ...(item.keywords || [])]
        .filter(Boolean)
        .map(normalizeKeyword)
        .join("\n");
      return haystack.includes(normalizedQuery);
    });
  }, [options, query]);

  const activeOptionId =
    open && activeIndex >= 0 && filteredOptions[activeIndex]
      ? `${generatedId}-option-${activeIndex}`
      : undefined;

  const openList = () => {
    if (disabled) {
      return;
    }
    setQuery("");
    setOpen(true);
    const selectedIndex = options.findIndex((item) => item.value === value);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : options.length ? 0 : -1);
  };

  const closeList = () => {
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  };

  const selectOption = (option: SearchableSelectOption) => {
    onChange(option.value);
    closeList();
    requestAnimationFrame(() => comboboxRef.current?.focus());
  };

  useEffect(() => {
    if (disabled && open) {
      closeList();
    }
  }, [disabled, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setActiveIndex((current) => {
      if (!filteredOptions.length) {
        return -1;
      }
      return current >= 0 && current < filteredOptions.length ? current : 0;
    });
  }, [filteredOptions, open]);

  useEffect(() => {
    if (!activeOptionId) {
      return;
    }
    const activeElement = document.getElementById(activeOptionId);
    activeElement?.scrollIntoView?.({ block: "nearest" });
  }, [activeOptionId]);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        closeList();
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  const moveActive = (direction: 1 | -1) => {
    if (!open) {
      openList();
      return;
    }
    if (!filteredOptions.length) {
      setActiveIndex(-1);
      return;
    }
    setActiveIndex((current) => {
      const start = current < 0 ? (direction === 1 ? -1 : 0) : current;
      return (start + direction + filteredOptions.length) % filteredOptions.length;
    });
  };

  return (
    <div className={`searchable-select${open ? " searchable-select-open" : ""}`} ref={rootRef}>
      <div className="searchable-select-control">
        <input
          ref={comboboxRef}
          type="text"
          className="searchable-select-trigger searchable-select-combobox"
          role="combobox"
          value={open ? query : selectedOption?.label || value}
          placeholder={open ? searchPlaceholder : placeholder}
          autoComplete="off"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-activedescendant={activeOptionId}
          disabled={disabled}
          onClick={() => {
            if (!open) {
              openList();
            }
          }}
          onChange={(event) => {
            if (!open) {
              setOpen(true);
            }
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              moveActive(1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              moveActive(-1);
            } else if (event.key === "Enter") {
              if (!open) {
                event.preventDefault();
                openList();
              } else if (activeIndex >= 0 && filteredOptions[activeIndex]) {
                event.preventDefault();
                selectOption(filteredOptions[activeIndex]);
              }
            } else if (event.key === "Escape" && open) {
              event.preventDefault();
              closeList();
            } else if (event.key === "Home" && open && filteredOptions.length) {
              event.preventDefault();
              setActiveIndex(0);
            } else if (event.key === "End" && open && filteredOptions.length) {
              event.preventDefault();
              setActiveIndex(filteredOptions.length - 1);
            }
          }}
        />
        <button
          type="button"
          className="searchable-select-toggle"
          aria-label={open ? "收起选项" : "展开选项"}
          aria-controls={listboxId}
          aria-expanded={open}
          disabled={disabled}
          tabIndex={-1}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            if (open) {
              closeList();
            } else {
              openList();
              requestAnimationFrame(() => comboboxRef.current?.focus());
            }
          }}
        >
          <span className="searchable-select-arrow" aria-hidden="true">
            ▾
          </span>
        </button>
      </div>

      {open && (
        <div className="searchable-select-dropdown">
          <div className="searchable-select-list" id={listboxId} role="listbox">
            {!options.length && <div className="searchable-select-empty">{emptyText}</div>}
            {!!options.length && !filteredOptions.length && (
              <div className="searchable-select-empty">{noResultsText}</div>
            )}
            {filteredOptions.map((item, index) => {
              const selected = item.value === value;
              const highlighted = index === activeIndex;
              return (
                <button
                  key={item.value}
                  id={`${generatedId}-option-${index}`}
                  type="button"
                  className={`searchable-select-option${selected ? " active" : ""}${highlighted ? " highlighted" : ""}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => selectOption(item)}
                  role="option"
                  aria-selected={selected}
                  tabIndex={-1}
                >
                  {item.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
