# FCT-DTI-X-01

Este repositório é a fonte de verdade da configuração do servidor Debian `FCT-DTI-X-01`.

Ele instala e mantém:

- Base do sistema Debian;
- Docker e Docker Compose;
- `ansible-pull`;
- Stacks Docker Compose em `/home/shared`;
- Configuração pública versionada;
- Overlays e segredos vindos do repositório privado.

Depois do bootstrap inicial, o próprio servidor reaplica este repositório automaticamente todos os dias às **04:00** pelo timer:

```bash
ansible-pull-fct-dti-x-01.timer
```

**O servidor não faz deploy direto da branch `main`**.
A branch de deploy é `production`, promovida automaticamente pelo GitHub Actions após validação.

Não altere `production` manualmente.

| Quero...                                     | Leia                                                                |
| -------------------------------------------- | ------------------------------------------------------------------- |
| Preparar o servidor pela primeira vez        | [Primeira execução](#primeira-execução)                             |
| Entender como o deploy funciona              | [Fluxo de deploy](#fluxo-de-deploy)                                 |
| Aplicar ou verificar mudanças no dia a dia   | [Operação diária](#operação-diária)                                 |
| Saber onde colocar Compose, dados e segredos | [Onde colocar cada coisa](#onde-colocar-cada-coisa)                 |
| Adicionar um novo stack Compose              | [Como adicionar um stack Compose](#como-adicionar-um-stack-compose) |
| Remover um stack Compose                     | [Como remover um stack Compose](#como-remover-um-stack-compose)     |
| Ver comandos de emergência ou de diagnóstico | [Comandos úteis](#comandos-úteis)                                   |

## Estrutura importante

```text
site.yml                         Playbook principal
inventory/hosts.yml              Inventário local
host_vars/fct-dti-x-01/           Configuração do servidor
roles/                            Tarefas Ansible
docker-compose/                   Stacks Compose públicos
docker-data/                      Configuração pública persistente
compose-overlays/                 Overlays de projetos externos
```

## Primeira execução

Logue no servidor com um usuário que possa usar `sudo`.

### 1. Mantenha acesso de recuperação

Confirme que existe pelo menos uma chave SSH de usuário não-root:

```bash
find /home -path '*/.ssh/authorized_keys' -type f -print
```

Se não houver chave, o hardening de SSH falha de propósito, para evitar que os usuários fiquem trancados, sem acesso.

### 2. Clone o repositório

```bash
cd /tmp
git clone https://github.com/cacic-fct/server-FCT-DTI-X-01.git
cd server-FCT-DTI-X-01
```

Para testar outra branch:

```bash
export BRANCH=nome-da-branch
```

### 3. Instale a chave do GitHub App de segredos

O repositório privado de segredos é:

```text
https://github.com/cacic-fct/server-FCT-DTI-X-01-secrets.git
```

Copie a chave privada do GitHub App para o caminho esperado:

```bash
sudo install -d -m 0750 /etc/github-secret

sudo install -m 0600 /caminho/da/chave.pem \
  /etc/github-secret/fct-dti-x-01-server-client.private-key.pem
```

Sem essa chave, o playbook deve falhar antes de aplicar os stacks Compose.

### 4. Rode o bootstrap

```bash
sudo ./scripts/bootstrap-ansible-pull.sh
```

O bootstrap instala o mínimo necessário e executa o primeiro `ansible-pull`.

Valores padrão:

```text
REPO_URL=https://github.com/cacic-fct/server-FCT-DTI-X-01.git
BRANCH=production
CHECKOUT=/opt/ansible-pull/server-FCT-DTI-X-01
```

Para testar outra branch ou fork:

```bash
sudo REPO_URL=https://github.com/cacic-fct/server-FCT-DTI-X-01.git \
  BRANCH=nome-da-branch \
  CHECKOUT=/opt/ansible-pull/server-FCT-DTI-X-01 \
  ./scripts/bootstrap-ansible-pull.sh
```

### 5. Verifique o timer

```bash
systemctl status ansible-pull-fct-dti-x-01.service
systemctl status ansible-pull-fct-dti-x-01.timer
systemctl list-timers ansible-pull-fct-dti-x-01.timer
```

Logs da última execução:

```bash
journalctl -u ansible-pull-fct-dti-x-01.service -n 200 --no-pager
```

Executar manualmente:

```bash
sudo systemctl start ansible-pull-fct-dti-x-01.service
```

### 6. Verifique Docker e Compose

```bash
docker ps
```

Listar projetos Compose aplicados:

```bash
find /home/shared/docker-compose -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

Validar um projeto específico:

```bash
cd /home/shared/docker-compose/traefik
docker compose config --quiet
docker compose ps
```

## Fluxo de deploy

1. Abra PR contra `main`.
2. Aguarde o GitHub Actions validar.
3. Depois do merge em `main`, o workflow promove o commit validado para `production`.
4. Faça o mesmo fluxo no repositório privado de segredos quando houver alteração em `.env`, `*.env` ou overlays privados.
5. O servidor aplica `production` dos dois repositórios no próximo timer ou quando o serviço for executado manualmente.

O CI valida:

- YAML;
- sintaxe Ansible;
- `ansible-lint`;
- `docker compose config --quiet` dos projetos Compose.

## Operação diária

Após alterar este repositório ou o repositório privado de segredos:

```bash
git add .
git commit -m "Descrição da mudança"
git push
```

Depois do deploy, acompanhe no servidor:

```bash
journalctl -u ansible-pull-fct-dti-x-01.service -f
```

Verifique containers:

```bash
docker ps
```

Verifique um Compose específico:

```bash
docker compose -f /home/shared/docker-compose/traefik/docker-compose.yml ps
```

## Onde colocar cada coisa

| Conteúdo                                       | Local                                                 |
| ---------------------------------------------- | ----------------------------------------------------- |
| Compose público                                | `docker-compose/<projeto>/`                           |
| Configuração pública versionada                | `docker-data/`                                        |
| `.env`, tokens e segredos                      | repositório privado `server-FCT-DTI-X-01-secrets`     |
| Estado de runtime                              | somente no servidor, em `/home/shared/docker-data`    |
| Projeto externo que continua como Git checkout | `compose_external_repositories` + `compose-overlays/` |

Não faça commit de bancos, uploads, caches persistentes, `acme.json`, `.env` ou arquivos com credenciais.

## Como adicionar um stack Compose

1. Crie:

```text
docker-compose/<projeto>/docker-compose.yml
```

2. Coloque segredos no repositório privado, não neste repositório.

3. Declare diretórios ou arquivos persistentes em:

```text
host_vars/fct-dti-x-01/60-compose-storage.yml
```

4. Declare redes externas, se necessário, em:

```text
host_vars/fct-dti-x-01/70-compose-networking.yml
```

5. Adicione o projeto em:

```text
host_vars/fct-dti-x-01/90-compose-projects.yml
```

6. Valide antes de enviar:

```bash
docker compose -f docker-compose/<projeto>/docker-compose.yml config --quiet
ansible-playbook --syntax-check site.yml
```

## Como remover um stack Compose

1. Remova o projeto de `compose_projects`.
2. Se quiser limpar containers e networks antigos, adicione o nome em `compose_retired_paths`.
3. Remova os arquivos públicos que não forem mais usados.
4. Remova overlays e segredos correspondentes no repositório privado.

Projetos em `compose_retired_paths` são removidos pelo label:

```text
com.docker.compose.project
```

## Modo conservador da primeira transição

A configuração inicial evita mudanças destrutivas:

```yaml
compose_pull_before_up: false
compose_remove_orphans: false
firewall_apply_ipv4_policy: false
server_require_non_root_ssh_authorized_key: true
```

Depois de validar a primeira execução, habilite uma mudança por vez:

```yaml
compose_pull_before_up: true
compose_remove_orphans: true
firewall_apply_ipv4_policy: true
```

Faça isso em commits pequenos.

## Comandos úteis

Executar Ansible manualmente:

```bash
sudo systemctl start ansible-pull-fct-dti-x-01.service
```

Ver logs:

```bash
journalctl -u ansible-pull-fct-dti-x-01.service -n 200 --no-pager
journalctl -u ansible-pull-fct-dti-x-01.service -f
```

Ver timers:

```bash
systemctl list-timers ansible-pull-fct-dti-x-01.timer
```

Ver containers:

```bash
docker ps
```

Validar Compose:

```bash
docker compose -f docker-compose/traefik/docker-compose.yml config --quiet
```

## Referências

- [FCT-DTI-X-01](https://cacic.com.br/docs/Recursos/Servidores/FCT-DTI-X-01)
- [Especificações comuns](https://cacic.com.br/docs/Recursos/Servidores/Especifica%C3%A7%C3%B5es%20comuns)
- [Organização de arquivos](https://cacic.com.br/docs/Recursos/Servidores/FCT-DTI-X-01/Organiza%C3%A7%C3%A3o%20de%20arquivos)
- [Firewall](https://cacic.com.br/docs/Recursos/Servidores/FCT-DTI-X-01/Rede/Firewall)
- [Pacotes](https://cacic.com.br/docs/Recursos/Servidores/FCT-DTI-X-01/HW%20e%20SW/Pacotes)
- [Sistema operacional](https://cacic.com.br/docs/Recursos/Servidores/FCT-DTI-X-01/HW%20e%20SW/Sistema%20operacional)
