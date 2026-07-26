import { useTranslation } from 'react-i18next'

export function useLocale() {
  const { t, i18n } = useTranslation()
  return { t, locale: i18n.language }
}