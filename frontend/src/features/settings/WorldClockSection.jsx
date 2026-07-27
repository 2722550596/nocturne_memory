import React, { useState, useEffect } from 'react';
import { Save, Clock, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from '../../components/Toast';

export default function WorldClockSection({ settings, onSave }) {
  const { t } = useTranslation();
  const [currentTime, setCurrentTime] = useState('2024-06-01');
  const [autoTimestamp, setAutoTimestamp] = useState(false);
  const [showRelative, setShowRelative] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings?.world_clock) {
      const clock = settings.world_clock;
      if (clock.current_time != null) setCurrentTime(clock.current_time);
      if (clock.auto_timestamp != null) setAutoTimestamp(clock.auto_timestamp);
      if (clock.show_relative != null) setShowRelative(clock.show_relative);
    }
  }, [settings?.world_clock]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const clock = {
        current_time: currentTime.trim(),
        auto_timestamp: autoTimestamp,
        show_relative: showRelative,
        format: settings?.world_clock?.format || 'YYYY-MM-DD'
      };
      await onSave({ world_clock: clock });
      setDirty(false);
    } catch (e) {
      toast((t('settings.world_clock.save_failed') || 'Save failed') + ': ' + (e.response?.data?.detail || e.message), "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5 pt-4">
      <div className="space-y-4">
        <div className="space-y-2">
          <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider">{t('settings.world_clock.current_time_label') || '当前世界时间'}</label>
          <input
            type="text"
            value={currentTime}
            onChange={e => { setCurrentTime(e.target.value); setDirty(true); }}
            placeholder="YYYY-MM-DD"
            className="bg-slate-950 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 text-sm w-full font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 shadow-inner"
          />
          <p className="text-[11px] text-slate-500">
            {t('settings.world_clock.current_time_hint') || '作为计算“N天前”的参考基准，也是自动计时的默认时间。'}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={autoTimestamp}
              onChange={e => { setAutoTimestamp(e.target.checked); setDirty(true); }}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-slate-400 after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-indigo-600 peer-checked:after:bg-white"></div>
          </label>
          <div>
            <span className="text-sm text-slate-300 block font-medium">{t('settings.world_clock.auto_timestamp_label') || '自动计时'}</span>
            <span className="text-xs text-slate-500 block">{t('settings.world_clock.auto_timestamp_desc') || '新记忆在未指定时间时，自动关联当前世界时间。'}</span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={showRelative}
              onChange={e => { setShowRelative(e.target.checked); setDirty(true); }}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-slate-400 after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-indigo-600 peer-checked:after:bg-white"></div>
          </label>
          <div>
            <span className="text-sm text-slate-300 block font-medium">{t('settings.world_clock.show_relative_label') || '显示相对时间'}</span>
            <span className="text-xs text-slate-500 block">{t('settings.world_clock.show_relative_desc') || '在预览记忆时显示类似“3天前”的相对日期提示。'}</span>
          </div>
        </div>
      </div>

      {dirty && (
        <div className="flex items-center justify-end pt-1">
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
          >
            <Save size={14} />
            {saving ? (t('settings.world_clock.saving') || '保存中...') : (t('settings.world_clock.save') || '保存更改')}
          </button>
        </div>
      )}
    </div>
  );
}
