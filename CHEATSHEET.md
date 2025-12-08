# LLAMATOR MCP - Cheat Sheet

Быстрая шпаргалка с командами для работы с LLAMATOR MCP.

## 📦 Установка

```bash
# Создать namespace
kubectl create namespace llamator-mcp

# Создать Secret с Vault credentials (userpass)
kubectl create secret generic vault-userpass-auth \
  --from-literal=username='llamator-mcp' \
  --from-literal=password='YOUR_PASSWORD' \
  --namespace llamator-mcp

# Установить VSO (если еще не установлен)
helm repo add hashicorp https://helm.releases.hashicorp.com
helm install vault-secrets-operator hashicorp/vault-secrets-operator \
  -n vault-secrets-operator-system --create-namespace

# Установить LLAMATOR MCP
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml
```

## 🔍 Проверка статуса

```bash
# Все ресурсы
kubectl get all -n llamator-mcp

# Поды
kubectl get pods -n llamator-mcp
kubectl get pods -n llamator-mcp -w  # watch mode

# Vault ресурсы
kubectl get vaultconnection -n llamator-mcp
kubectl get vaultauth -n llamator-mcp
kubectl get vaultstaticsecret -n llamator-mcp

# Секреты
kubectl get secrets -n llamator-mcp

# HPA
kubectl get hpa -n llamator-mcp

# Ingress
kubectl get ingress -n llamator-mcp
```

## 📝 Логи

```bash
# API логи
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=api -f

# Worker логи
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=worker -f

# Redis логи
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=redis -f

# Логи конкретного пода
kubectl logs -n llamator-mcp <pod-name> -f

# Логи VSO
kubectl logs -n vault-secrets-operator-system \
  -l app.kubernetes.io/name=vault-secrets-operator -f

# Предыдущие логи (если под перезапустился)
kubectl logs -n llamator-mcp <pod-name> --previous
```

## 🔧 Describe (детальная информация)

```bash
# Под
kubectl describe pod <pod-name> -n llamator-mcp

# Deployment
kubectl describe deployment llamator-llamator-mcp-api -n llamator-mcp

# VaultStaticSecret
kubectl describe vaultstaticsecret -n llamator-mcp

# Ingress
kubectl describe ingress -n llamator-mcp

# Events
kubectl get events -n llamator-mcp --sort-by='.lastTimestamp'
```

## 🔄 Управление

```bash
# Restart deployment
kubectl rollout restart deployment/llamator-llamator-mcp-api -n llamator-mcp
kubectl rollout restart deployment/llamator-llamator-mcp-worker -n llamator-mcp

# Статус rollout
kubectl rollout status deployment/llamator-llamator-mcp-api -n llamator-mcp

# История rollout
kubectl rollout history deployment/llamator-llamator-mcp-api -n llamator-mcp

# Scale вручную
kubectl scale deployment/llamator-llamator-mcp-api --replicas=5 -n llamator-mcp

# Обновить Helm release
helm upgrade llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml

# Откатить
helm rollback llamator -n llamator-mcp

# История
helm history llamator -n llamator-mcp
```

## 🔐 Vault (через CLI)

```bash
# Настроить окружение
export VAULT_ADDR=https://vault.kubepractice.ru

# Войти
vault login -method=userpass username=<your-username>

# Прочитать секрет
vault kv get secret/team11/llamator-mcp/openai

# Обновить секрет
vault kv put secret/team11/llamator-mcp/openai \
  attack_api_key='new-key'

# Список секретов
vault kv list secret/team11/llamator-mcp

# История версий
vault kv metadata get secret/team11/llamator-mcp/openai
```

## 🧪 Тестирование

```bash
# Health check через Ingress
curl https://llamator.kubepractice.ru/v1/health

# Port-forward для локального теста
kubectl port-forward -n llamator-mcp svc/llamator-llamator-mcp-api 8000:8000
curl http://localhost:8000/v1/health

# Exec в контейнер
kubectl exec -it <pod-name> -n llamator-mcp -- /bin/sh

# Проверить environment variables в поде
kubectl exec -it <pod-name> -n llamator-mcp -- env | grep LLAMATOR

# Проверить подключение к Redis
kubectl exec -it <redis-pod> -n llamator-mcp -- redis-cli ping
```

## 📊 Мониторинг

```bash
# Использование ресурсов
kubectl top pods -n llamator-mcp
kubectl top nodes

# HPA метрики
kubectl get hpa -n llamator-mcp -w

# Describe HPA
kubectl describe hpa llamator-llamator-mcp-api -n llamator-mcp
```

## 🗑️ Удаление

```bash
# Удалить release
helm uninstall llamator -n llamator-mcp

# Удалить PVC (если нужно)
kubectl delete pvc -n llamator-mcp --all

# Удалить namespace
kubectl delete namespace llamator-mcp

# Удалить VSO (если нужно)
helm uninstall vault-secrets-operator -n vault-secrets-operator-system
kubectl delete namespace vault-secrets-operator-system
```

## 🐛 Debug

```bash
# Проверить NetworkPolicy
kubectl describe networkpolicy -n llamator-mcp

# Проверить ServiceAccount
kubectl get sa llamator-mcp-sa -n llamator-mcp
kubectl describe sa llamator-mcp-sa -n llamator-mcp

# Проверить RBAC
kubectl get role -n llamator-mcp
kubectl get rolebinding -n llamator-mcp
kubectl describe role llamator-llamator-mcp-role -n llamator-mcp

# Проверить ConfigMap
kubectl get configmap -n llamator-mcp
kubectl describe configmap llamator-llamator-mcp-config -n llamator-mcp

# Проверить PVC
kubectl get pvc -n llamator-mcp
kubectl describe pvc -n llamator-mcp

# Проверить доступность Vault
curl -k https://vault.kubepractice.ru/v1/sys/health
```

## 📝 Полезные алиасы

Добавьте в `~/.bashrc` или `~/.zshrc`:

```bash
# Алиасы для kubectl
alias k='kubectl'
alias kgp='kubectl get pods'
alias kgs='kubectl get svc'
alias kgd='kubectl get deployments'
alias kdp='kubectl describe pod'
alias kl='kubectl logs -f'
alias ke='kubectl exec -it'

# Алиасы для llamator namespace
alias kn='kubectl -n llamator-mcp'
alias knp='kubectl get pods -n llamator-mcp'
alias knl='kubectl logs -n llamator-mcp -f'

# Helm алиасы
alias h='helm'
alias hl='helm list'
alias hi='helm install'
alias hu='helm upgrade'
```

## 🔗 Быстрые ссылки

- **Vault UI:** https://vault.kubepractice.ru
- **API:** https://llamator.kubepractice.ru
- **Health:** https://llamator.kubepractice.ru/v1/health

## 📚 Документация

- `DEPLOYMENT-GUIDE.md` - инструкция по развертыванию
- `llamator-helm/VAULT-USERPASS.md` - настройка Vault
- `llamator-helm/README.md` - полная документация Helm
- `llamator-helm/SECRETS.md` - управление секретами

