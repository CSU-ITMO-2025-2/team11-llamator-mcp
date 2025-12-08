# LLAMATOR MCP - Deployment Guide для Team11

Краткая инструкция по развертыванию LLAMATOR MCP в вашем Kubernetes кластере с Vault.

## 🎯 Ваша инфраструктура

- **Vault:** https://vault.kubepractice.ru (userpass auth)
- **Ingress:** llamator.kubepractice.ru
- **API Provider:** api.vsegpt.ru
- **Cluster:** kubepractice.ru

---

## 🚀 Быстрое развертывание (5 шагов)

### Шаг 1: Настроить секреты в Vault

Войдите в Vault UI: https://vault.kubepractice.ru

Создайте следующие секреты:

**Path: `secret/team11/llamator-mcp/openai`**
```
attack_api_key: <ваш API ключ от api.vsegpt.ru>
judge_api_key: <ваш API ключ от api.vsegpt.ru>
target_api_key: <ваш API ключ от api.vsegpt.ru>
```

**Path: `secret/team11/llamator-mcp/api`**
```
api_key: <придумайте secure ключ для защиты вашего API>
```

### Шаг 2: Настроить Vault Policy

```bash
# В Vault UI или через CLI создайте policy:
# Secrets > Policies > Create Policy

# Name: llamator-mcp
# Policy:
path "secret/data/team11/llamator-mcp/*" {
  capabilities = ["read", "list"]
}

path "secret/metadata/team11/llamator-mcp/*" {
  capabilities = ["read", "list"]
}
```

### Шаг 3: Создать userpass пользователя в Vault

```bash
# В Vault UI: Access > Auth Methods > userpass > Create user

Username: llamator-mcp
Password: <придумайте secure password>
Policies: llamator-mcp

# Сохраните username и password - понадобятся для K8s Secret!
```

### Шаг 4: Установить Vault Secrets Operator

```bash
# Добавить Helm repo
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

# Установить VSO
helm install vault-secrets-operator hashicorp/vault-secrets-operator \
  --namespace vault-secrets-operator-system \
  --create-namespace

# Проверить
kubectl get pods -n vault-secrets-operator-system
```

### Шаг 5: Развернуть LLAMATOR MCP

```bash
# 1. Создать namespace
kubectl create namespace llamator-mcp

# 2. Создать Secret с Vault credentials
kubectl create secret generic vault-userpass-auth \
  --from-literal=username='llamator-mcp' \
  --from-literal=password='<password из шага 3>' \
  --namespace llamator-mcp

# 3. Установить Helm chart
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml

# 4. Проверить развертывание
kubectl get pods -n llamator-mcp -w
```

---

## ✅ Проверка работы

```bash
# 1. Проверить все ресурсы
kubectl get all -n llamator-mcp

# 2. Проверить что Vault синхронизировал секреты
kubectl get vaultstaticsecret -n llamator-mcp
kubectl get secrets -n llamator-mcp | grep llamator

# 3. Проверить логи API
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=api -f

# 4. Проверить логи Worker
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=worker -f

# 5. Проверить Ingress
kubectl get ingress -n llamator-mcp

# 6. Тест API
curl https://llamator.kubepractice.ru/v1/health
```

---

## 🔧 Кастомизация

Если нужно изменить конфигурацию, создайте файл `values-custom.yaml`:

```yaml
# values-custom.yaml

# Изменить количество реплик
api:
  replicaCount: 3
  autoscaling:
    minReplicas: 3
    maxReplicas: 15

worker:
  replicaCount: 5
  autoscaling:
    minReplicas: 3
    maxReplicas: 20

# Изменить resources
api:
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"

# Изменить storage
redis:
  persistence:
    size: 20Gi

artifacts:
  persistence:
    size: 50Gi
```

Установка с кастомными values:

```bash
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml \
  -f values-custom.yaml
```

---

## 🔄 Обновление

```bash
# Обновить chart
helm upgrade llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml

# Откатить к предыдущей версии
helm rollback llamator -n llamator-mcp

# История релизов
helm history llamator -n llamator-mcp
```

---

## 🐛 Troubleshooting

### Поды не запускаются

```bash
# Проверить события
kubectl get events -n llamator-mcp --sort-by='.lastTimestamp'

# Describe конкретный под
kubectl describe pod <pod-name> -n llamator-mcp

# Проверить логи
kubectl logs <pod-name> -n llamator-mcp
```

### Секреты не синхронизируются из Vault

```bash
# Проверить VaultAuth
kubectl describe vaultauth -n llamator-mcp

# Проверить VaultStaticSecret
kubectl describe vaultstaticsecret -n llamator-mcp

# Проверить логи VSO
kubectl logs -n vault-secrets-operator-system \
  -l app.kubernetes.io/name=vault-secrets-operator --tail=50

# Проверить Secret с credentials
kubectl get secret vault-userpass-auth -n llamator-mcp -o yaml
```

### API недоступен

```bash
# Проверить Ingress
kubectl describe ingress -n llamator-mcp

# Проверить Service
kubectl get svc -n llamator-mcp

# Проверить NetworkPolicy
kubectl describe networkpolicy -n llamator-mcp

# Port-forward для локального теста
kubectl port-forward -n llamator-mcp svc/llamator-llamator-mcp-api 8000:8000
curl http://localhost:8000/v1/health
```

---

## 📚 Дополнительная документация

- **📖 [Полная документация Helm](llamator-helm/README.md)**
- **🔐 [Vault UserPass Setup](llamator-helm/VAULT-USERPASS.md)** - детальная инструкция
- **🔒 [Secrets Management](llamator-helm/SECRETS.md)** - все методы управления секретами
- **🚀 [Quick Start](llamator-helm/QUICKSTART.md)** - быстрый старт без Vault

---

## 🎓 Для команды

### Для CI/CD (участник 3)

Готово к интеграции:
- ArgoCD manifests в `llamator-helm/CICD-INTEGRATION.md`
- Image будет: `ilchoss/llamator-mcp:v0.1.0`
- Можно использовать GitOps для автоматического деплоя

### Для Observability (участник 4)

Готово к мониторингу:
- Metrics endpoint: `/metrics`
- Health check: `/v1/health`
- Prometheus annotations уже настроены
- См. `values.yaml` секция `monitoring`

---

## 📞 Контакты

**Ответственный за Helm:** Участник 2 (K8s/Helm/Безопасность)

**Вопросы:**
- Настройка Helm-чарта
- Проблемы с Vault интеграцией
- Настройка безопасности (RBAC, NetworkPolicy)
- Масштабирование (HPA, PDB)

---

## ✨ Финальный чеклист

Перед демонстрацией проверьте:

- [ ] Vault доступен и секреты созданы
- [ ] Vault Secrets Operator установлен
- [ ] Все поды в статусе Running
- [ ] Секреты синхронизированы из Vault
- [ ] API доступен через Ingress: https://llamator.kubepractice.ru
- [ ] Health check работает: `/v1/health`
- [ ] Логи без ошибок
- [ ] HPA настроен и работает
- [ ] Redis persistence работает
- [ ] NetworkPolicy не блокирует трафик

---

**🎉 Готово к деплою!**

