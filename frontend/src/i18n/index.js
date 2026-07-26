import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './en.json'
import zh from './zh.json'
import api from '../lib/api'

i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  lng: 'en',
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false,
  },
})

i18n.on('languageChanged', (lng) => {
  api.defaults.headers.common['Accept-Language'] = lng
})

const SUPPORTED = ['en', 'zh']

function detectBrowserLocale() {
  const primary = navigator.language?.split('-')[0]?.toLowerCase()
  return SUPPORTED.includes(primary) ? primary : 'en'
}

export async function detectLocale() {
  try {
    const res = await api.get('/settings')
    const locale = res.data.settings?.locale
    if (locale) {
      await i18n.changeLanguage(locale)
      return locale
    }
  } catch {}

  // config has no locale set — detect from browser
  const detected = detectBrowserLocale()
  await i18n.changeLanguage(detected)
  return detected
}

export default i18n