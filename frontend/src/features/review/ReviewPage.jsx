import React, { useEffect, useState, useRef } from 'react';
import { getGroups, getGroupDiff, rollbackGroup, approveGroup, clearAll, getRevisions, checkoutRevision } from '../../lib/api';
import SnapshotList from '../../components/SnapshotList';
import DiffViewer from '../../components/DiffViewer';
import { toast } from '../../components/Toast';
import ConfirmModal from '../../components/ConfirmModal';
import {
  Activity,
  Check,
  FileText,
  Layout,
  RotateCcw,
  ShieldCheck,
  Database,
  Trash2,
  Box,
  Link as LinkIcon,
  BookOpen,
  GitBranch,
} from 'lucide-react';
import clsx from 'clsx';
import { useLocale } from '../../i18n/useLocale';

function ReviewPage() {
  const { t } = useLocale();
  const [changes, setChanges] = useState([]);
  const [selectedChange, setSelectedChange] = useState(null);
  const [diffData, setDiffData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [diffError, setDiffError] = useState(null);
  const [confirmState, setConfirmState] = useState(null);
  const [viewMode, setViewMode] = useState('review');  // 'review' | 'history'
  const [revisions, setRevisions] = useState([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [checkoutError, setCheckoutError] = useState(null);

  const diffRequestRef = useRef(0);

  useEffect(() => { loadChanges(); }, []);

  const loadRevisions = async () => {
    setRevisionsLoading(true);
    setCheckoutError(null);
    try {
      const data = await getRevisions();
      setRevisions(data?.revisions || []);
    } catch {
      setRevisions([]);
    } finally {
      setRevisionsLoading(false);
    }
  };

  useEffect(() => {
    if (viewMode === 'history') loadRevisions();
  }, [viewMode]);

  const handleCheckout = (revisionId) => {
    setConfirmState({
      title: t('review.confirm.checkout_title', { defaultValue: 'Checkout Revision' }),
      message: t('review.confirm.checkout_message', { defaultValue: 'Rewind HEAD to this revision? This creates a new branch node; current state is preserved in the tree.' }),
      variant: "default",
      confirmLabel: t('review.confirm.checkout_label', { defaultValue: 'Checkout' }),
      onConfirm: async () => {
        setConfirmState(null);
        try {
          await checkoutRevision(revisionId);
          await loadRevisions();
        } catch (err) {
          const detail = err.response?.data?.detail || err.message;
          setCheckoutError(detail);
          toast(t('review.toast.checkout_failed', { defaultValue: 'Checkout failed', error: detail }), "error");
        }
      },
      onCancel: () => setConfirmState(null),
    });
  };

  const loadChanges = async () => {
    setLoading(true);
    try {
      const list = await getGroups();
      setChanges(Array.isArray(list) ? list : []);
      if (selectedChange && !list.find(c => c.node_uuid === selectedChange.node_uuid)) {
        setSelectedChange(list.length > 0 ? list[0] : null);
      } else if (list.length > 0 && !selectedChange) {
        setSelectedChange(list[0]);
      }
      if (list.length === 0) {
        setSelectedChange(null);
        setDiffData(null);
      }
      return list;
    } catch {
      setDiffError(t('review.error.disconnected'));
      return [];
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (selectedChange) {
      loadDiff(selectedChange.node_uuid);
    }
  }, [selectedChange]);

  const loadDiff = async (nodeUuid) => {
    const requestId = ++diffRequestRef.current;
    setDiffError(null);
    setDiffData(null);
    try {
      const data = await getGroupDiff(nodeUuid);
      if (requestId === diffRequestRef.current) setDiffData(data);
    } catch (err) {
      if (requestId === diffRequestRef.current) {
        setDiffError(err.response?.data?.detail || t('review.error.retrieve_failed'));
        setDiffData(null);
      }
    }
  };

  const handleRollback = () => {
    if (!selectedChange) return;
    setConfirmState({
      title: t('review.confirm.reject_title'),
      message: t('review.confirm.reject_message', { uri: selectedChange.display_uri }),
      variant: "danger",
      confirmLabel: t('review.confirm.reject_label'),
      onConfirm: async () => {
        setConfirmState(null);
        try {
          const res = await rollbackGroup(selectedChange.node_uuid);
          if (res && res.success === false) {
            throw new Error(res.message || t('review.error.unknown_rollback'));
          }
          const list = await loadChanges();
          if (list.find(c => c.node_uuid === selectedChange.node_uuid)) {
            await loadDiff(selectedChange.node_uuid);
          }
        } catch (err) {
          const errorMsg = err.response?.data?.detail || err.message;
          toast(t('review.toast.rejection_failed', { error: errorMsg }), "error");
        }
      },
      onCancel: () => setConfirmState(null),
    });
  };

  const handleApprove = async () => {
    if (!selectedChange) return;
    try {
      await approveGroup(selectedChange.node_uuid);
      await loadChanges();
    } catch (err) {
      toast(t('review.toast.integration_failed', { error: err.message }), "error");
    }
  };

  const handleClearAll = () => {
    setConfirmState({
      title: t('review.confirm.integrate_all_title'),
      message: t('review.confirm.integrate_all_message'),
      variant: "default",
      confirmLabel: t('review.confirm.integrate_all_label'),
      onConfirm: async () => {
        setConfirmState(null);
        try {
          await clearAll();
          setChanges([]);
          setSelectedChange(null);
          setDiffData(null);
        } catch (err) {
          toast(t('review.toast.mass_integration_failed', { error: err.message }), "error");
        }
      },
      onCancel: () => setConfirmState(null),
    });
  };

  const renderMetadataChanges = () => {
    if (!diffData?.before_meta || !diffData?.current_meta) return null;
    const metaKeys = ['priority', 'disclosure'];
    
    const hasPathChanges = diffData.path_changes && diffData.path_changes.length > 0;
    
    const diffs = metaKeys.filter(key => {
      const oldVal = diffData.before_meta[key];
      const newVal = diffData.current_meta[key];
      const isChanged = JSON.stringify(oldVal) !== JSON.stringify(newVal);
      
      if (isChanged) return true;
      if (hasPathChanges && (oldVal != null || newVal != null)) return true;
      
      return false;
    });

    if (diffs.length === 0) return null;

    const allPreserved = diffs.every(key => JSON.stringify(diffData.before_meta[key]) === JSON.stringify(diffData.current_meta[key]));
    const isCreation = diffData.action === 'created';
    const isDeletion = diffData.current_meta.priority == null && diffData.before_meta.priority != null;

    return (
      <div className="mb-8 p-4 bg-slate-900/40 border border-slate-800/60 rounded-lg backdrop-blur-sm">
        <h3 className="text-xs font-bold text-slate-500 uppercase mb-4 flex items-center gap-2 tracking-widest">
          <Activity size={12} /> {t('review.metadata.title')} {isCreation ? t('review.metadata.initial') : isDeletion ? t('review.metadata.removed') : allPreserved ? t('review.metadata.preserved') : t('review.metadata.shifts')}
        </h3>
        <div className="space-y-3">
          {diffs.map(key => {
            const oldVal = diffData.before_meta[key];
            const newVal = diffData.current_meta[key];
            const isChanged = JSON.stringify(oldVal) !== JSON.stringify(newVal);
            
            return (
              <div key={key} className="grid grid-cols-[100px_1fr_20px_1fr] gap-4 text-sm items-start">
                <span className="text-slate-400 font-medium capitalize text-xs pt-0.5">{key}</span>
                <div className={clsx("text-xs font-mono text-right break-words", isChanged && !isCreation ? "text-rose-400/70 line-through" : "text-slate-500")}>
                  {oldVal != null ? String(oldVal) : '∅'}
                </div>
                <div className="text-center text-slate-700 pt-0.5">
                  {isChanged ? '→' : '≡'}
                </div>
                <div className={clsx("text-xs font-mono font-bold break-words", isChanged ? "text-emerald-400" : "text-slate-400")}>
                  {newVal != null ? String(newVal) : '∅'}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  const changeTypeIcon = (type) => {
    switch (type) {
      case 'nodes': return <Box size={18} />;
      case 'memories': return <FileText size={18} />;
      case 'edges': return <LinkIcon size={18} />;
      case 'paths': return <Database size={18} />;
      case 'glossary_keywords': return <BookOpen size={18} />;
      default: return <FileText size={18} />;
    }
  };

  const changeTypeStyle = (action) => {
    switch (action) {
      case 'created':
        return "bg-emerald-950/10 border-emerald-500/20 text-emerald-400 shadow-[0_0_15px_rgba(16,185,129,0.1)]";
      case 'deleted':
        return "bg-rose-950/10 border-rose-500/20 text-rose-400 shadow-[0_0_15px_rgba(244,63,94,0.1)]";
      default:
        return "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_15px_rgba(245,158,11,0.1)]";
    }
  };

  return (
    <div className="flex h-full bg-[#05050A] text-slate-300 overflow-hidden font-sans selection:bg-purple-500/30 selection:text-purple-200">

      {/* Sidebar */}
      <div className="w-72 flex-shrink-0 flex flex-col border-r border-slate-800/30 bg-[#08080E]">
        <div className="p-5 border-b border-slate-800/30">
          <div className="flex items-center gap-3 text-slate-100">
            <div className="w-8 h-8 rounded bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-purple-900/20">
              <ShieldCheck className="w-4 h-4 text-white" />
            </div>
            <div className="flex flex-col">
              <span className="font-bold tracking-tight text-sm">{t('review.sidebar.title')}</span>
              <span className="text-[10px] text-indigo-400/70 uppercase tracking-widest font-medium">{t('review.sidebar.subtitle')}</span>
            </div>
          </div>
        </div>
        <div className="flex border-b border-slate-800/30">
          <button
            onClick={() => setViewMode('review')}
            className={clsx(
              "flex-1 flex items-center justify-center gap-1.5 py-2.5 text-[11px] font-medium uppercase tracking-wider transition-all",
              viewMode === 'review'
                ? "text-indigo-300 border-b border-indigo-500/50 bg-indigo-500/5"
                : "text-slate-600 hover:text-slate-400 border-b border-transparent"
            )}
          >
            <ShieldCheck size={13} /> {t('review.tab.review', { defaultValue: 'Review' })}
          </button>
          <button
            onClick={() => setViewMode('history')}
            className={clsx(
              "flex-1 flex items-center justify-center gap-1.5 py-2.5 text-[11px] font-medium uppercase tracking-wider transition-all",
              viewMode === 'history'
                ? "text-purple-300 border-b border-purple-500/50 bg-purple-500/5"
                : "text-slate-600 hover:text-slate-400 border-b border-transparent"
            )}
          >
            <GitBranch size={13} /> {t('review.tab.history', { defaultValue: 'History' })}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto py-2">
          {viewMode === 'history' ? (
            revisionsLoading ? (
              <div className="p-8 flex justify-center">
                <div className="w-6 h-6 border-2 border-purple-500/30 border-t-purple-500 rounded-full animate-spin"></div>
              </div>
            ) : revisions.length === 0 ? (
              <div className="p-6 text-center text-xs text-slate-600">
                {t('review.history.empty', { defaultValue: 'No revisions yet. Approve or rollback changes to build the tree.' })}
              </div>
            ) : (
              <div className="px-2">
                {revisions.map((rev) => (
                  <div
                    key={rev.id}
                    className={clsx(
                      "group flex items-start gap-2 px-3 py-2 rounded-md cursor-default transition-colors",
                      rev.is_head ? "bg-purple-500/10 border border-purple-500/20" : "hover:bg-slate-800/30"
                    )}
                  >
                    <div className={clsx(
                      "mt-0.5 w-2 h-2 rounded-full flex-shrink-0",
                      rev.author === 'admin' ? "bg-rose-400" : "bg-indigo-400"
                    )} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-mono text-slate-300">#{rev.id}</span>
                        <span className={clsx(
                          "text-[9px] px-1 rounded uppercase tracking-wider",
                          rev.author === 'admin' ? "bg-rose-500/10 text-rose-400" : "bg-indigo-500/10 text-indigo-400"
                        )}>
                          {rev.author}
                        </span>
                        {rev.is_head && (
                          <span className="text-[9px] px-1 rounded bg-purple-500/20 text-purple-300 uppercase tracking-wider font-bold">HEAD</span>
                        )}
                      </div>
                      <div className="text-[10px] text-slate-600 mt-0.5 truncate">
                        {rev.created_at ? new Date(rev.created_at).toLocaleString() : '—'}
                      </div>
                      {rev.message && (
                        <div className="text-[10px] text-slate-500 mt-0.5 truncate">{rev.message}</div>
                      )}
                    </div>
                    {!rev.is_head && (
                      <button
                        onClick={() => handleCheckout(rev.id)}
                        title={t('review.action.checkout', { defaultValue: 'Checkout to this revision' })}
                        className="opacity-0 group-hover:opacity-100 transition-opacity text-slate-500 hover:text-purple-300 p-1"
                      >
                        <RotateCcw size={12} />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )
          ) : loading ? (
            <div className="p-8 flex justify-center">
              <div className="w-6 h-6 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin"></div>
            </div>
          ) : (
            <SnapshotList
              snapshots={changes}
              selectedId={selectedChange?.node_uuid}
              onSelect={setSelectedChange}
            />
          )}
        </div>

        {viewMode === 'review' && changes.length > 0 && (
          <div className="p-4 border-t border-slate-800/30 bg-slate-900/20 backdrop-blur-sm">
            <button
              onClick={handleClearAll}
              className="w-full group flex items-center justify-center gap-2 bg-slate-800/50 hover:bg-emerald-900/20 text-slate-400 hover:text-emerald-400 border border-slate-700 hover:border-emerald-800/50 rounded-md py-2.5 text-xs font-medium transition-all duration-300"
            >
              <Check size={14} className="group-hover:scale-110 transition-transform" />
              <span>{t('review.action.integrate_all')}</span>
            </button>
          </div>
        )}
      </div>

      {/* Main Stage */}
      <div className="flex-1 flex flex-col min-w-0 bg-[#05050A] relative">
        <div className="absolute top-0 left-0 right-0 h-96 bg-gradient-to-b from-purple-900/5 to-transparent pointer-events-none" />

        {viewMode === 'history' ? (
          <div className="flex-1 overflow-y-auto px-8 py-8 relative z-10">
            <div className="max-w-3xl mx-auto">
              <div className="flex items-center gap-3 mb-6">
                <GitBranch size={20} className="text-purple-400" />
                <h2 className="text-lg font-medium text-slate-100">{t('review.history.title', { defaultValue: 'Revision History' })}</h2>
              </div>
              {checkoutError && (
                <div className="mb-4 p-3 rounded-md bg-rose-950/20 border border-rose-800/40 text-rose-300 text-xs">
                  {checkoutError}
                </div>
              )}
              {revisions.length === 0 ? (
                <p className="text-sm text-slate-600">{t('review.history.empty', { defaultValue: 'No revisions yet. Approve or rollback changes to build the tree.' })}</p>
              ) : (
                <div className="space-y-1">
                  {revisions.map((rev) => (
                    <div
                      key={rev.id}
                      className={clsx(
                        "flex items-center gap-4 p-3 rounded-md border transition-colors",
                        rev.is_head
                          ? "bg-purple-500/5 border-purple-500/30"
                          : "bg-slate-900/30 border-slate-800/40 hover:border-slate-700"
                      )}
                    >
                      <div className={clsx(
                        "w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-mono font-bold",
                        rev.author === 'admin'
                          ? "bg-rose-500/10 text-rose-400 border border-rose-500/20"
                          : "bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                      )}>
                        {rev.author === 'admin' ? 'A' : 'AI'}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-mono text-slate-200">#{rev.id}</span>
                          {rev.parent_id && <span className="text-[10px] text-slate-600">← #{rev.parent_id}</span>}
                          {rev.is_head && <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 uppercase tracking-wider font-bold">HEAD</span>}
                        </div>
                        <div className="text-xs text-slate-500 mt-0.5">
                          {rev.created_at ? new Date(rev.created_at).toLocaleString() : '-'}
                          {rev.message ? ` · ${rev.message}` : ''}
                        </div>
                      </div>
                      {!rev.is_head && (
                        <button
                          onClick={() => handleCheckout(rev.id)}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800/50 hover:bg-purple-900/20 text-slate-400 hover:text-purple-300 border border-slate-700 hover:border-purple-700/50 rounded-md text-[11px] font-medium transition-all"
                        >
                          <RotateCcw size={12} /> {t('review.action.checkout', { defaultValue: 'Checkout' })}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : selectedChange ? (
          <>
            {/* Header */}
            <div className="h-20 border-b border-slate-800/30 flex items-center justify-between px-8 relative z-10 backdrop-blur-sm">
              <div className="flex items-center gap-4 min-w-0">
                <div className={clsx(
                  "w-10 h-10 rounded-full flex items-center justify-center border",
                  changeTypeStyle(selectedChange.action)
                )}>
                  {changeTypeIcon(selectedChange.top_level_table)}
                </div>
                <div className="min-w-0 flex flex-col">
                  <h2 className="text-lg font-medium text-slate-100 truncate tracking-tight flex items-center gap-3">
                    <span>{selectedChange.display_uri}</span>
                    {selectedChange.namespaces && selectedChange.namespaces.length > 0 && selectedChange.namespaces.some(ns => ns !== "" || selectedChange.namespaces.length > 1) && (
                      <span className="text-[10px] px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 tracking-widest font-mono uppercase">
                        {selectedChange.namespaces.map(ns => ns === "" ? t('review.namespace.default') : ns).join(', ')}
                      </span>
                    )}
                  </h2>
                  <div className="flex items-center gap-2 text-xs text-slate-500">
                    <span className="bg-slate-800/50 px-1.5 py-0.5 rounded text-slate-400 capitalize">
                      {selectedChange.top_level_table} {selectedChange.action || t('review.badge.modified')}
                    </span>
                    <span className="text-slate-600">
                      ({t('review.badge.rows_affected', { count: selectedChange.row_count })})
                    </span>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <button
                  onClick={handleRollback}
                  className="flex items-center gap-2 px-5 py-2 bg-slate-900 hover:bg-rose-950/30 border border-slate-700 hover:border-rose-800 text-slate-400 hover:text-rose-400 rounded-md transition-all duration-200 text-xs font-medium uppercase tracking-wider"
                >
                  <RotateCcw size={14} /> {t('review.action.reject_group')}
                </button>
                <button
                  onClick={handleApprove}
                  className="flex items-center gap-2 px-6 py-2 bg-indigo-600/10 hover:bg-indigo-500/20 border border-indigo-500/30 hover:border-indigo-500/50 text-indigo-300 hover:text-indigo-200 rounded-md transition-all duration-200 text-xs font-bold uppercase tracking-wider shadow-[0_0_15px_rgba(99,102,241,0.1)] hover:shadow-[0_0_20px_rgba(99,102,241,0.2)]"
                >
                  <Check size={14} /> {t('review.action.integrate_group')}
                </button>
              </div>
            </div>

            {/* Diff Area */}
            <div className="flex-1 overflow-y-auto px-8 py-8 custom-scrollbar">
              <div className="max-w-4xl mx-auto">
                {diffError ? (
                  <div className="mt-20 flex flex-col items-center justify-center text-rose-500 gap-6 animate-in fade-in zoom-in duration-300">
                    <div className="w-20 h-20 bg-rose-950/20 rounded-full flex items-center justify-center border border-rose-900/50 shadow-xl">
                      <Activity size={32} />
                    </div>
                    <div className="text-center">
                      <p className="text-lg font-medium text-rose-200">{t('review.error.retrieval_failed_title')}</p>
                      <p className="text-rose-400/60 mt-2 max-w-md text-sm">{diffError}</p>
                    </div>
                    <button
                      onClick={() => loadDiff(selectedChange.node_uuid)}
                      className="px-6 py-2 bg-slate-800/50 hover:bg-slate-800 rounded-full text-slate-300 text-xs transition-colors border border-slate-700"
                    >
                      {t('review.action.retry')}
                    </button>
                  </div>
                ) : diffData ? (
                  <div className="animate-in fade-in slide-in-from-bottom-4 duration-500">
                    <div className="mb-6 flex justify-end">
                      <div className={clsx(
                        "inline-flex items-center gap-2 px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest border",
                        diffData.action === 'deleted' 
                          ? "bg-rose-500/5 border-rose-500/20 text-rose-500" 
                          : diffData.action === 'created'
                            ? "bg-emerald-500/5 border-emerald-500/20 text-emerald-500"
                            : (diffData.has_changes || diffData.path_changes?.length > 0 || diffData.glossary_changes?.length > 0)
                              ? "bg-amber-500/5 border-amber-500/20 text-amber-500"
                              : "bg-slate-800/50 border-slate-700 text-slate-500"
                      )}>
                        {diffData.action === 'deleted' ? t('review.diff_status.deletion_detected') 
                          : diffData.action === 'created' ? t('review.diff_status.creation_detected') 
                          : (diffData.has_changes || diffData.path_changes?.length > 0 || diffData.glossary_changes?.length > 0) ? t('review.diff_status.modification_detected') 
                          : t('review.diff_status.no_deviation')}
                      </div>
                    </div>

                    {diffData.path_changes && diffData.path_changes.length > 0 && (
                      <div className="mb-8 p-4 bg-slate-900/40 border border-slate-800/60 rounded-lg backdrop-blur-sm">
                        <h3 className="text-xs font-bold text-slate-500 uppercase mb-4 flex items-center gap-2 tracking-widest">
                          <Database size={12} /> {t('review.path_modifications.title')}
                        </h3>
                        <div className="space-y-2">
                          {diffData.path_changes.map((pc, i) => (
                            <div key={i} className="flex items-center gap-3 text-sm">
                              {pc.action === 'deleted' ? (
                                <span className="text-rose-500/80 bg-rose-500/10 px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider">{t('review.path.removed')}</span>
                              ) : (
                                <span className="text-emerald-500/80 bg-emerald-500/10 px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider">{t('review.path.added')}</span>
                              )}
                              <span className={clsx("font-mono text-xs break-all", pc.action === 'deleted' ? "text-rose-400/70 line-through" : "text-emerald-400")}>
                                {pc.uri}
                              </span>
                              {pc.namespace !== undefined && pc.namespace !== null && (pc.namespace !== "" || (selectedChange.namespaces && selectedChange.namespaces.some(n => n !== "" || selectedChange.namespaces.length > 1))) && (
                                    <span className="ml-auto text-[10px] px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 tracking-wider font-mono">
                                      {pc.namespace === "" ? t('review.namespace.default') : pc.namespace}
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                        {diffData.active_paths && diffData.active_paths.length > 0 && (
                          <div className="mt-4 pt-4 border-t border-slate-800/50">
                            <span className="text-xs text-slate-500 block mb-2">{t('review.path.accessible_at')}</span>
                            <div className="flex flex-wrap gap-2">
                              {diffData.active_paths.map((uri, i) => (
                                <span key={i} className="flex items-center gap-2 text-xs font-mono text-indigo-300 bg-indigo-900/10 border border-indigo-500/20 px-2 py-1 rounded">
                                  <span>{uri}</span>
                                  {diffData.path_namespaces && diffData.path_namespaces[uri] && diffData.path_namespaces[uri]
                                    .filter(ns => ns !== "" || diffData.path_namespaces[uri].length > 1 || (selectedChange.namespaces && selectedChange.namespaces.some(n => n !== "" || selectedChange.namespaces.length > 1)))
                                    .map((ns, nsIdx) => (
                                    <span key={nsIdx} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                                      {ns === "" ? t('review.namespace.default') : ns}
                                    </span>
                                  ))}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {diffData.glossary_changes && diffData.glossary_changes.length > 0 && (
                      <div className="mb-8 p-4 bg-slate-900/40 border border-slate-800/60 rounded-lg backdrop-blur-sm">
                        <h3 className="text-xs font-bold text-slate-500 uppercase mb-4 flex items-center gap-2 tracking-widest">
                          <BookOpen size={12} /> {t('review.glossary.title')}
                        </h3>
                        <div className="space-y-2">
                          {diffData.glossary_changes.map((gc, i) => (
                            <div key={i} className="flex items-center gap-3 text-sm">
                              {gc.action === 'deleted' ? (
                                <span className="text-rose-500/80 bg-rose-500/10 px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider">{t('review.path.removed')}</span>
                              ) : (
                                <span className="text-emerald-500/80 bg-emerald-500/10 px-2 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider">{t('review.path.added')}</span>
                              )}
                              <span className={clsx("font-mono text-xs break-all", gc.action === 'deleted' ? "text-rose-400/70 line-through" : "text-emerald-400")}>
                                {gc.keyword}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {renderMetadataChanges()}

                    <div className="bg-[#0A0A12]/50 rounded-xl border border-slate-800/50 p-1 min-h-[200px] shadow-2xl relative overflow-hidden">
                      <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-transparent via-indigo-500/20 to-transparent opacity-50"></div>
                      <div className="p-6 md:p-10">
                        <DiffViewer
                          oldText={diffData.before_content ?? ''}
                          newText={diffData.current_content ?? ''}
                        />
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center h-64 text-slate-700">
                    <div className="w-2 h-2 bg-indigo-500 rounded-full animate-ping mb-4"></div>
                    <span className="text-xs tracking-widest uppercase opacity-50">{t('review.status.synchronizing')}</span>
                  </div>
                )}
              </div>
            </div>
          </>
        ) : diffError ? (
          <div className="flex-1 flex flex-col items-center justify-center text-rose-500 gap-4">
            <Activity size={48} className="opacity-20" />
            <p className="text-sm font-medium opacity-50">{t('review.status.connection_lost')}</p>
          </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-slate-700 gap-6 select-none">
            <div className="relative">
              <div className="absolute inset-0 bg-indigo-500/20 blur-3xl rounded-full opacity-20 animate-pulse"></div>
              <Layout size={64} className="opacity-20 relative z-10" />
            </div>
            <div className="text-center">
              <p className="text-lg font-light text-slate-500">{t('review.empty.awaiting_input')}</p>
              <p className="text-xs text-slate-600 mt-2 tracking-wide uppercase">{t('review.empty.select_memory')}</p>
            </div>
          </div>
        )}
      </div>
      {confirmState && <ConfirmModal {...confirmState} />}
    </div>
  );
}

export default ReviewPage;
