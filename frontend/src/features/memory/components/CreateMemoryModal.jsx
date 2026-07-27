import React, { useState, useRef, useEffect } from 'react';
import { Plus, Loader2 } from 'lucide-react';
import { createMemory } from '../../../lib/api';
import { useLocale } from '../../../i18n/useLocale';

export default function CreateMemoryModal({ onClose, onCreated, parentPath, currentDomain }) {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [priority, setPriority] = useState(0);
  const [disclosure, setDisclosure] = useState('');
  const [worldTimestamp, setWorldTimestamp] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const { t } = useLocale();
  const textareaRef = useRef(null);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [content]);

  const handleCreate = async () => {
    if (!content.trim() || !disclosure.trim()) return;
    setSaving(true);
    setError('');
    try {
      const result = await createMemory({
        parent_path: parentPath,
        content: content.trim(),
        priority,
        disclosure: disclosure.trim(),
        title: title.trim() || undefined,
        domain: currentDomain,
        world_timestamp: worldTimestamp.trim() || undefined,
      });
      onCreated(result.uri);
      // Reset form
      setTitle('');
      setContent('');
      setPriority(0);
      setDisclosure('');
      setWorldTimestamp('');
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleClose = () => {
    if (saving) return;
    setTitle('');
    setContent('');
    setPriority(0);
    setDisclosure('');
    setWorldTimestamp('');
    setError('');
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
      onClick={handleClose}
    >
      <div
        className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-[#0C0C14] border border-slate-800 rounded-xl p-6 max-w-4xl w-[calc(100%-2rem)] shadow-2xl max-h-[90vh] overflow-y-auto custom-scrollbar"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2.5 rounded-lg bg-indigo-950/40 text-indigo-400">
            <Plus size={20} />
          </div>
          <div>
            <h3 className="text-base font-bold text-slate-100">{t('memory.create.title')}</h3>
            <p className="text-xs text-slate-500 mt-0.5">{t('memory.create.subtitle')}</p>
          </div>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-rose-950/20 border border-rose-900/30 rounded-lg text-rose-400 text-sm">
            {error}
          </div>
        )}

        <div className="space-y-5">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {/* Parent path (readonly) */}
            <div className="space-y-1.5 md:col-span-2">
              <label className="text-xs font-medium text-slate-400">{t('memory.create.parent_path')}</label>
              <div className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-indigo-300/70 font-mono select-all">
                {currentDomain}://{parentPath || 'root'}
              </div>
            </div>

            {/* Title */}
            <div className="space-y-1.5">
              <label className="flex items-baseline justify-between">
                <span className="text-xs font-medium text-slate-400">
                  {t('memory.create.title_label')} <span className="text-slate-600 font-normal">{t('memory.create.optional')}</span>
                </span>
                <span className="text-[10px] text-slate-600">{t('memory.create.title_hint')}</span>
              </label>
              <input
                type="text"
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder={t('memory.create.title_placeholder')}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 font-mono focus:outline-none focus:border-indigo-500/50 transition-colors"
              />
            </div>

            {/* Priority */}
            <div className="space-y-1.5">
              <label className="flex items-baseline justify-between">
                <span className="text-xs font-medium text-slate-400">{t('memory.create.priority_label')}</span>
                <span className="text-[10px] text-slate-600">{t('memory.create.priority_hint')}</span>
              </label>
              <input
                type="number"
                min="0"
                value={priority}
                onChange={e => setPriority(parseInt(e.target.value) || 0)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 font-mono focus:outline-none focus:border-indigo-500/50 transition-colors"
              />
            </div>

            {/* World Timestamp */}
            <div className="space-y-1.5">
              <label className="flex items-baseline justify-between">
                <span className="text-xs font-medium text-slate-400">
                  {t('memory.create.world_timestamp_label') || '世界观时间'} <span className="text-slate-600 font-normal">{t('memory.create.optional')}</span>
                </span>
              </label>
              <input
                type="text"
                value={worldTimestamp}
                onChange={e => setWorldTimestamp(e.target.value)}
                placeholder="YYYY-MM-DD"
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-700 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500/50 transition-all"
              />
            </div>
          </div>

          {/* Disclosure */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-400">
              {t('memory.create.disclosure_label')} <span className="text-rose-400">*</span>
            </label>
            <input
              type="text"
              value={disclosure}
              onChange={e => setDisclosure(e.target.value)}
              placeholder={t('memory.create.disclosure_placeholder')}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-indigo-500/50 transition-colors"
            />
          </div>

          {/* Content */}
          <div className="space-y-1.5 flex flex-col">
            <label className="text-xs font-medium text-slate-400">
              {t('memory.create.content_label')} <span className="text-rose-400">*</span>
            </label>
            <textarea
              ref={textareaRef}
              value={content}
              onChange={e => setContent(e.target.value)}
              placeholder={t('memory.create.content_placeholder')}
              className="w-full min-h-[120px] bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 font-mono focus:outline-none focus:border-indigo-500/50 transition-colors resize-none overflow-hidden"
              spellCheck={false}
            />
          </div>
        </div>

        <div className="flex gap-3 justify-end mt-6 pt-4 border-t border-slate-800/50">
          <button
            onClick={handleClose}
            disabled={saving}
            className="px-4 py-2 text-sm text-slate-400 hover:text-slate-200 bg-slate-800 hover:bg-slate-700 rounded-lg border border-slate-700 transition-colors disabled:opacity-50"
          >
            {t('memory.create.cancel')}
          </button>
          <button
            onClick={handleCreate}
            disabled={saving || !content.trim() || !disclosure.trim()}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg transition-colors shadow-lg shadow-indigo-900/20 disabled:opacity-50"
          >
            {saving ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                {t('memory.create.creating')}
              </>
            ) : (
              <>
                <Plus size={16} />
                {t('memory.create.button')}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
