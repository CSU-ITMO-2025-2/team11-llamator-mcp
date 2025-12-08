# Vault UserPass Authentication Setup

Руководство по настройке LLAMATOR MCP с существующим Vault на https://vault.kubepractice.ru используя UserPass аутентификацию.

## Обзор

Ваш Vault уже развернут:
- **URL:** https://vault.kubepractice.ru
- **Auth Method:** userpass (username/password)
- **KV Mount:** secret (предполагаем KV v2)

## 🚀 Быстрый старт

### Шаг 1: Создать UserPass учетную запись в Vault (если еще нет)

```bash
# Войти в Vault UI
https://vault.kubepractice.ru

# Или через CLI (если есть доступ)
export VAULT_ADDR=https://vault.kubepractice.ru
vault login -method=userpass username=<your-username>

# Создать пользователя для llamator-mcp (требуется admin права)
vault write auth/userpass/users/llamator-mcp \
  password="secure-password-here" \
  policies="llamator-mcp"
```

### Шаг 2: Создать Policy в Vault

```bash
# Создать policy для доступа к секретам
vault policy write llamator-mcp - <<EOF
# Read secrets for llamator-mcp
path "secret/data/team11/llamator-mcp/*" {
  capabilities = ["read", "list"]
}

# Read metadata
path "secret/metadata/team11/llamator-mcp/*" {
  capabilities = ["read", "list"]
}
EOF
```

### Шаг 3: Создать секреты в Vault

#### Через Web UI:

1. Перейти на https://vault.kubepractice.ru
2. Login с вашими credentials
3. Перейти в **Secrets > secret**
4. Создать path: `team11/llamator-mcp/openai`
5. Добавить ключи:
   - `attack_api_key`: ваш API ключ
   - `judge_api_key`: ваш API ключ
   - `target_api_key`: ваш API ключ
6. Создать path: `team11/llamator-mcp/api`
7. Добавить ключ:
   - `api_key`: ваш secure API key

#### Через CLI:

```bash
# Войти
export VAULT_ADDR=https://vault.kubepractice.ru
vault login -method=userpass username=<your-username>

# Создать OpenAI secrets
vault kv put secret/team11/llamator-mcp/openai \
  attack_api_key='your-attack-key' \
  judge_api_key='your-judge-key' \
  target_api_key='your-target-key'

# Создать API key
vault kv put secret/team11/llamator-mcp/api \
  api_key='your-secure-api-key'

# Проверить
vault kv get secret/team11/llamator-mcp/openai
```

### Шаг 4: Создать Kubernetes Secret с Vault credentials

```bash
# Создать Secret с userpass credentials для Vault Secrets Operator
kubectl create secret generic vault-userpass-auth \
  --from-literal=username='llamator-mcp' \
  --from-literal=password='secure-password-here' \
  --namespace llamator-mcp
```

### Шаг 5: Обновить VaultAuth для userpass

Создайте файл `vault-auth-userpass.yaml`:

```yaml
apiVersion: secrets.hashicorp.com/v1beta1
kind: VaultAuth
metadata:
  name: llamator-llamator-mcp-vault-auth
  namespace: llamator-mcp
spec:
  # UserPass auth method
  method: userpass
  mount: userpass
  
  # Параметры userpass
  params:
    username: llamator-mcp
  
  # Ссылка на Secret с паролем
  userPass:
    secretRef: vault-userpass-auth
  
  # Ссылка на VaultConnection
  vaultConnectionRef: vault-connection-external
```

Применить:
```bash
kubectl apply -f vault-auth-userpass.yaml
```

### Шаг 6: Установить Vault Secrets Operator (если еще не установлен)

```bash
helm install vault-secrets-operator hashicorp/vault-secrets-operator \
  --namespace vault-secrets-operator-system \
  --create-namespace
```

### Шаг 7: Установить Helm Chart

```bash
# Используя готовый values файл
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  --create-namespace \
  -f llamator-helm/values-vault-external.yaml

# Или с кастомизацией
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  --create-namespace \
  --set vault.enabled=true \
  --set vault.address=https://vault.kubepractice.ru \
  --set vault.vaultSecretsOperator.enabled=true \
  --set vault.vaultSecretsOperator.authMount=userpass \
  --set vault.vaultSecretsOperator.secretPath=team11/llamator-mcp \
  --set ingress.hosts[0].host=llamator.kubepractice.ru
```

## 📋 Проверка работы

```bash
# 1. Проверить VaultConnection
kubectl get vaultconnection -n llamator-mcp
kubectl describe vaultconnection vault-connection-external -n llamator-mcp

# 2. Проверить VaultAuth
kubectl get vaultauth -n llamator-mcp
kubectl describe vaultauth -n llamator-mcp

# 3. Проверить VaultStaticSecret
kubectl get vaultstaticsecret -n llamator-mcp
kubectl describe vaultstaticsecret llamator-llamator-mcp-openai-keys -n llamator-mcp

# 4. Проверить что Kubernetes Secrets созданы
kubectl get secrets -n llamator-mcp | grep llamator

# 5. Проверить логи VSO
kubectl logs -n vault-secrets-operator-system \
  -l app.kubernetes.io/name=vault-secrets-operator -f

# 6. Проверить деплойменты
kubectl get pods -n llamator-mcp
kubectl logs -n llamator-mcp -l app.kubernetes.io/component=api
```

## 🔄 Обновление секретов

```bash
# 1. Обновить секрет в Vault через UI или CLI
export VAULT_ADDR=https://vault.kubepractice.ru
vault login -method=userpass username=llamator-mcp

vault kv put secret/team11/llamator-mcp/openai \
  attack_api_key='new-key-value'

# 2. VSO автоматически синхронизирует изменения
# Deployments перезапустятся автоматически

# Проверить статус
kubectl get vaultstaticsecret -n llamator-mcp -w
kubectl rollout status deployment -n llamator-mcp
```

## 🛠️ Альтернативный метод: Прямое создание секретов

Если VSO недоступен или вы хотите простой setup:

```bash
# 1. Получить секреты из Vault вручную
export VAULT_ADDR=https://vault.kubepractice.ru
vault login -method=userpass username=<your-user>

ATTACK_KEY=$(vault kv get -field=attack_api_key secret/team11/llamator-mcp/openai)
JUDGE_KEY=$(vault kv get -field=judge_api_key secret/team11/llamator-mcp/openai)
TARGET_KEY=$(vault kv get -field=target_api_key secret/team11/llamator-mcp/openai)
API_KEY=$(vault kv get -field=api_key secret/team11/llamator-mcp/api)

# 2. Создать Kubernetes Secrets вручную
kubectl create secret generic llamator-openai-keys \
  --from-literal=attack-api-key="$ATTACK_KEY" \
  --from-literal=judge-api-key="$JUDGE_KEY" \
  --from-literal=target-api-key="$TARGET_KEY" \
  --namespace llamator-mcp

kubectl create secret generic llamator-api-key \
  --from-literal=api-key="$API_KEY" \
  --namespace llamator-mcp

# 3. Установить Helm без Vault integration
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  --create-namespace \
  --set vault.enabled=false
```

## 🔐 Security Best Practices

### 1. Ротация паролей

```bash
# Изменить пароль userpass пользователя
vault write auth/userpass/users/llamator-mcp/password \
  password="new-secure-password"

# Обновить Kubernetes Secret
kubectl create secret generic vault-userpass-auth \
  --from-literal=username='llamator-mcp' \
  --from-literal=password='new-secure-password' \
  --namespace llamator-mcp \
  --dry-run=client -o yaml | kubectl apply -f -

# Перезапустить VSO или поды
kubectl rollout restart deployment -n vault-secrets-operator-system
```

### 2. Используйте минимальные permissions

Policy должна давать только read доступ к необходимым путям:

```hcl
path "secret/data/team11/llamator-mcp/*" {
  capabilities = ["read", "list"]
}

# НЕ давайте write/delete без необходимости!
```

### 3. Audit logging

Убедитесь что в Vault включен audit log для отслеживания доступа к секретам.

## 🐛 Troubleshooting

### VaultAuth не может аутентифицироваться

```bash
# Проверить Secret с credentials
kubectl get secret vault-userpass-auth -n llamator-mcp -o yaml

# Проверить VaultAuth status
kubectl describe vaultauth -n llamator-mcp

# Проверить логи VSO
kubectl logs -n vault-secrets-operator-system \
  -l app.kubernetes.io/name=vault-secrets-operator --tail=50

# Тест аутентификации вручную
vault login -method=userpass username=llamator-mcp
```

### VaultConnection не может подключиться

```bash
# Проверить доступность Vault
curl -k https://vault.kubepractice.ru/v1/sys/health

# Проверить VaultConnection
kubectl describe vaultconnection vault-connection-external -n llamator-mcp

# Проверить network policy
kubectl get networkpolicy -n llamator-mcp
```

### Секреты не синхронизируются

```bash
# Проверить что секреты существуют в Vault
vault kv get secret/team11/llamator-mcp/openai

# Проверить VaultStaticSecret status
kubectl describe vaultstaticsecret -n llamator-mcp

# Проверить policy permissions
vault policy read llamator-mcp

# Проверить path в VaultStaticSecret
kubectl get vaultstaticsecret -n llamator-mcp -o yaml
```

## 📚 Структура секретов

```
Vault (https://vault.kubepractice.ru)
└── secret/data/
    └── team11/
        └── llamator-mcp/
            ├── openai
            │   ├── attack_api_key
            │   ├── judge_api_key
            │   └── target_api_key
            └── api
                └── api_key
```

## 🔗 Полезные ссылки

- [Vault UI](https://vault.kubepractice.ru)
- [UserPass Auth Method Docs](https://developer.hashicorp.com/vault/docs/auth/userpass)
- [Vault Secrets Operator](https://developer.hashicorp.com/vault/docs/platform/k8s/vso)

## ✅ Checklist перед деплоем

- [ ] Vault доступен: https://vault.kubepractice.ru
- [ ] Создан userpass пользователь: `llamator-mcp`
- [ ] Создана policy: `llamator-mcp`
- [ ] Policy назначена пользователю
- [ ] Секреты созданы в Vault:
  - [ ] `secret/team11/llamator-mcp/openai`
  - [ ] `secret/team11/llamator-mcp/api`
- [ ] Kubernetes Secret создан: `vault-userpass-auth`
- [ ] Vault Secrets Operator установлен
- [ ] Namespace создан: `llamator-mcp`

После выполнения всех шагов можно устанавливать Helm chart!

```bash
helm install llamator ./llamator-helm \
  -n llamator-mcp \
  -f llamator-helm/values-vault-external.yaml
```

