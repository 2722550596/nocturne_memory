import React, { useState, useEffect } from 'react';
import { Save, AlertTriangle, RefreshCw, Copy, Eye, EyeOff } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import { toast } from '../../components/Toast';

export default function AdvancedSection({ settings, lockedFields = [], onSave }) {
  const { t } = useTranslation();
  const isLocked = (field) => lockedFields.includes(field);
  const [host, setHost] = useState('127.0.0.1');
  const [token, setToken] = useState('');
  const [readonlyMcp, setReadonlyMcp] = useState(false);
  const [skipBackup, setSkipBackup] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings?.host != null) setHost(settings.host);
    if (settings?.public_readonly_mcp != null) setReadonlyMcp(settings.public_readonly_mcp);
    if (settings?.skip_migration_backup != null) setSkipBackup(settings.skip_migration_backup);
  }, [settings?.host, settings?.api_token, settings?.public_readonly_mcp, settings?.skip_migration_backup]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const updates = { public_readonly_mcp: readonlyMcp, skip_migration_backup: skipBackup };
      if (!isLocked('host')) updates.host = host.trim();
      if (token.trim()) updates.api_token = token.trim();
      await onSave(updates);
      setDirty(false);
    } catch (e) {
      toast(t('settings.advanced.save_failed') + ': ' + (e.response?.data?.detail || e.message), "error");
    } finally {
      setSaving(false);
    }
  };

  const isRemote = host.trim() !== '127.0.0.1' && host.trim() !== 'localhost' && host.trim() !== '::1';

  return (
    <div className="space-y-5 pt-4">
      <div className="space-y-3">
        <div className="space-y-2">
          <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider">{t('settings.advanced.host_label')}</label>
          <input
            type="text"
            value={host}
            onChange={e => { setHost(e.target.value); setDirty(true); }}
            disabled={isLocked('host')}
            placeholder={t('settings.advanced.host_placeholder')}
            className="bg-slate-950 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 text-sm w-full font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 shadow-inner disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {isLocked('host') ? (
            <p className="text-[11px] text-slate-500">{t('settings.advanced.docker_managed')}</p>
          ) : (
            <p className="text-[11px] text-slate-500">
              {isRemote
                ? t('settings.advanced.remote_mode_hint')
                : <Trans i18nKey="settings.advanced.localhost_hint" components={{ c: <code className="text-slate-400" /> }}>
                    Default: only this machine can connect. Change to <c>0.0.0.0</c> to allow LAN / remote access.
                  </Trans>
              }
            </p>
          )}
        </div>

        {isRemote && (
          <div className="space-y-2">
            <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider">{t('settings.advanced.token_label')}</label>
            <div className="flex gap-1.5">
              <div className="relative flex-1">
                <input
                  type={showToken ? 'text' : 'password'}
                  value={token}
                  onChange={e => { setToken(e.target.value); setDirty(true); }}
                  placeholder={settings?.api_token ? t('settings.advanced.token_already_set') : t('settings.advanced.token_not_set')}
                  className="bg-slate-950 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 pr-9 text-sm w-full font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 shadow-inner"
                />
                {token && (
                  <button
                    type="button"
                    onClick={() => setShowToken(!showToken)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 transition-colors"
                  >
                    {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  const arr = new Uint8Array(32);
                  crypto.getRandomValues(arr);
                  const generated = Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
                  setToken(generated);
                  setDirty(true);
                }}
                className="px-2.5 py-2 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg text-xs flex items-center gap-1.5 transition-colors flex-shrink-0"
                title={t('settings.advanced.generate_token_title')}
              >
                <RefreshCw size={12} /> {t('settings.advanced.generate')}
              </button>
              {token && (
                <button
                  type="button"
                  onClick={() => { navigator.clipboard.writeText(token); }}
                  className="px-2.5 py-2 bg-slate-700 hover:bg-slate-600 text-slate-200 rounded-lg text-xs flex items-center gap-1.5 transition-colors flex-shrink-0"
                  title={t('settings.advanced.copy_token_title')}
                >
                  <Copy size={12} />
                </button>
              )}
            </div>
            <p className="text-[11px] text-slate-500">
              <Trans i18nKey="settings.advanced.token_hint" components={{ code: <code className="text-slate-400" /> }}>
                Clients must send <code>Authorization: Bearer &lt;token&gt;</code> to access the API.
              </Trans>
            </p>
            {!settings?.api_token && !token.trim() && (
              <p className="text-[11px] text-amber-400 flex items-center gap-1">
                <AlertTriangle size={11} /> {t('settings.advanced.token_required')}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="border-t border-slate-800/50 pt-4">
        <div className="flex items-center gap-3">
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={readonlyMcp}
              onChange={e => { setReadonlyMcp(e.target.checked); setDirty(true); }}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-slate-400 after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-red-600 peer-checked:after:bg-white"></div>
          </label>
          <div>
            <span className="text-sm text-slate-300 block font-medium">{t('settings.advanced.readonly_label')}</span>
            <span className="text-xs text-slate-500 block">{t('settings.advanced.readonly_desc')}</span>
          </div>
        </div>
      </div>
      <div className="border-t border-slate-800/50 pt-4">
        <div className="flex items-center gap-3">
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={skipBackup}
              onChange={e => { setSkipBackup(e.target.checked); setDirty(true); }}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-slate-400 after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-amber-600 peer-checked:after:bg-white"></div>
          </label>
          <div>
            <span className="text-sm text-slate-300 block font-medium">{t('settings.advanced.skip_backup_label')}</span>
            <span className="text-xs text-slate-500 block">{t('settings.advanced.skip_backup_desc')}</span>
          </div>
        </div>
      </div>

      {dirty && (
        <div className="flex items-center justify-between pt-1">
          <p className="text-xs text-amber-400 flex items-center gap-1">
            <AlertTriangle size={12} /> {t('settings.advanced.restart_warning')}
          </p>
          <button
            onClick={handleSave}
            disabled={saving || (isRemote && !token.trim() && !settings?.api_token)}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
          >
            <Save size={14} />
            {saving ? t('settings.advanced.saving') : t('settings.advanced.save')}
          </button>
        </div>
      )}
    </div>
  );
}
