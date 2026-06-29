# FCT-DTI-X-01

Este repositório é a fonte de verdade da configuração do servidor Debian
`FCT-DTI-X-01`. Ele instala a base do sistema, Docker, `ansible-pull` e os
stacks Docker Compose que rodam em `/home/shared`.

Depois do primeiro bootstrap, o próprio servidor reaplica este repositório todos
os dias às 06:00 e às 18:00 pelo timer
`ansible-pull-fct-dti-x-01.timer`.

O servidor não acompanha a `main` diretamente. Os arquivos são validados e
promovidos automaticamente para `production`, que é a branch de deploy e não
deve ser alterada manualmente.

## O que este repositório gerencia

- Sistema base: pacotes, atualizações automáticas, memória, mensagens de login,
  política de acesso e firewall.
- Docker: repositório APT oficial, pacotes e serviço.
- `ansible-pull`: checkout em `/opt/ansible-pull/server-FCT-DTI-X-01` e units
  systemd para reaplicação periódica.
- Compose público: arquivos versionados em `docker-compose/`.
- Configuração pública de dados: arquivos versionados em `docker-data/`.
- Overlays de projetos externos: arquivos em `compose-overlays/`.
- Segredos: arquivos vindos do repositório privado companheiro, copiados por
  cima da árvore pública antes da validação e do deploy.

O playbook principal é `site.yml`. O inventário é local e fica em
`inventory/hosts.yml`.

## Estrutura rápida

- `host_vars/SECOMPP/00-server.yml`: identidade do servidor, caminhos base
  e contas humanas esperadas.
- `host_vars/SECOMPP/10-ansible-pull.yml`: origem, branch, checkout e
  agenda do `ansible-pull`.
- `host_vars/SECOMPP/20-packages.yml`: pacotes obrigatórios e opcionais.
- `host_vars/SECOMPP/30-firewall.yml`: portas públicas e redes autorizadas
  para SSH/ICMP.
- `host_vars/SECOMPP/50-secrets.yml`: repositório privado de segredos e
  autenticação via GitHub App.
- `host_vars/SECOMPP/60-compose-storage.yml`: diretórios e arquivos de
  estado persistente em `/home/shared/docker-data`.
- `host_vars/SECOMPP/70-compose-networking.yml`: redes Docker externas.
- `host_vars/SECOMPP/80-compose-repositories.yml`: projetos que precisam
  continuar como checkouts Git no servidor.
- `host_vars/SECOMPP/90-compose-projects.yml`: lista dos projetos Compose
  aplicados e flags conservadoras de transição.
- `roles/`: implementação das tarefas Ansible.
- `docs/compose-inventory.md`: inventário dos Compose migrados do servidor.

## Primeira execução, passo a passo

Este roteiro assume que você está logado no servidor `FCT-DTI-X-01` com um
usuário que consegue usar `sudo`.

### 1. Confirme acesso antes de mexer

Abra uma sessão SSH e mantenha outra sessão ou console de recuperação disponível
enquanto aplica a primeira vez. A política IPv4 do firewall começa desligada
por segurança, mas a primeira execução ainda mexe em pacotes, Docker, Compose e
systemd.

Confirme que existe pelo menos uma chave SSH de usuário não-root:

```bash
find /home -path '*/.ssh/authorized_keys' -type f -print
```

O hardening de SSH falha de propósito se não houver chave em
`/home/*/.ssh/authorized_keys`.

### 2. Baixe este repositório no servidor

Use um diretório temporário qualquer. Exemplo:

```bash
cd /tmp
git clone https://github.com/cacic-fct/server-FCT-DTI-X-01.git
cd server-FCT-DTI-X-01
```

Se estiver testando uma branch diferente da `main`, exporte `BRANCH` antes do
bootstrap:

```bash
export BRANCH=nome-da-branch
```

### 3. Prepare a chave do GitHub App de segredos

O repositório privado de segredos é:

```text
https://github.com/cacic-fct/server-FCT-DTI-X-01-secrets.git
```

Antes de rodar o Ansible, coloque a chave privada do GitHub App no caminho
esperado:

```bash
sudo install -d -m 0750 /etc/github-secret
sudo install -m 0600 /caminho/da/chave.pem \
  /etc/github-secret/fct-dti-x-01-server-client.private-key.pem
```

O Ansible cria o usuário de sistema `github-secret`, ajusta dono/permissões da
chave e usa tokens temporários do GitHub App para:

- Clonar ou atualizar `/home/shared/server-FCT-DTI-X-01-secrets`;
- Autenticar o Docker em `ghcr.io` quando necessário.

Sem essa chave, o playbook deve falhar antes de aplicar os stacks Compose,
porque não há como validar os overlays privados.

### 4. Rode o bootstrap

Execute como root:

```bash
sudo ./scripts/bootstrap-ansible-pull.sh
```

O script faz o mínimo necessário para o primeiro `ansible-pull`:

1. Instala `ansible`, `git`, Python e certificados;
2. Instala a coleção `community.docker`;
3. Cria `/opt/ansible-pull`;
4. Roda `ansible-pull` apontando para este repositório e para `site.yml`.

Por padrão, ele usa:

```text
REPO_URL=https://github.com/cacic-fct/server-FCT-DTI-X-01.git
BRANCH=production
CHECKOUT=/opt/ansible-pull/server-FCT-DTI-X-01
```

Para testar outro fork, branch ou checkout:

```bash
sudo REPO_URL=https://github.com/cacic-fct/server-FCT-DTI-X-01.git \
  BRANCH=nome-da-branch \
  CHECKOUT=/opt/ansible-pull/server-FCT-DTI-X-01 \
  ./scripts/bootstrap-ansible-pull.sh
```

### 5. Verifique se o timer ficou ativo

Depois do bootstrap:

```bash
systemctl status ansible-pull-fct-dti-x-01.service
systemctl status ansible-pull-fct-dti-x-01.timer
systemctl list-timers ansible-pull-fct-dti-x-01.timer
```

Para ver logs:

```bash
journalctl -u ansible-pull-fct-dti-x-01.service -n 200 --no-pager
```

Para forçar uma nova aplicação manual depois do bootstrap:

```bash
sudo systemctl start ansible-pull-fct-dti-x-01.service
```

### 6. Valide Docker e Compose

Confira os containers:

```bash
docker ps
```

Confira os projetos aplicados:

```bash
find /home/shared/docker-compose -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

Valide um projeto específico:

```bash
cd /home/shared/docker-compose/traefik
docker compose config --quiet
docker compose ps
```

O Ansible já roda `docker compose config --quiet` para cada projeto listado em
`compose_projects` antes de subir os stacks.

## Validação e promoção

O GitHub Actions roda em pull requests e em pushes para `main` e `production`.
Ele valida:

- Sintaxe YAML de todos os arquivos `.yml` e `.yaml`;
- Sintaxe Ansible com `ansible-playbook --syntax-check site.yml`;
- `ansible-lint`;
- `docker compose config --quiet` para cada projeto em `docker-compose/` e
  `compose-overlays/`.

Para validar Compose sem segredos no CI, `scripts/validate-compose-config.sh`
copia os projetos para um diretório temporário, cria arquivos `env_file`
vazios e fornece valores placeholder somente para a etapa de parsing.

Fluxo de produção:

1. Abra PRs contra `main`.
2. Depois do merge em `main`, aguarde o GitHub Actions passar.
3. Se a validação passar, o workflow atualiza `production` automaticamente para
   o mesmo commit validado.

Na primeira vez, crie a branch de produção a partir da `main` já validada:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b production
git push -u origin production
```

O `ansible-pull` do servidor usa `production`, então o deploy só acontece após
o workflow promover um commit validado.

## Modo conservador da primeira transição

A configuração inicial evita mudanças destrutivas ou difíceis de reverter:

- `compose_pull_before_up: false`: não puxa imagens novas automaticamente; usa
  imagem local quando existir e só baixa quando estiver faltando.
- `compose_remove_orphans: false`: não remove containers órfãos dos projetos
  ativos.
- `firewall_apply_ipv4_policy: false`: não aplica a política IPv4 até alguém
  confirmar acesso SSH/console.
- `server_require_non_root_ssh_authorized_key: true`: impede hardening de SSH
  se não houver chave de usuário não-root.

Quando a primeira execução estiver validada, habilite explicitamente as
mudanças em `host_vars/SECOMPP/`:

```yaml
compose_pull_before_up: true
compose_remove_orphans: true
firewall_apply_ipv4_policy: true
```

Faça essas mudanças em commits pequenos e aplique uma de cada vez se o servidor
ainda estiver em migração.

## Como gerenciar estado, configuração e segredos

Use esta regra antes de adicionar qualquer arquivo:

| Tipo de conteúdo                                           | Onde fica                                             | Exemplo                                           |
| ---------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------- |
| Compose público                                            | `docker-compose/<projeto>/`                           | `docker-compose/traefik/docker-compose.yml`       |
| Configuração pública montada em `/home/shared/docker-data` | `docker-data/`                                        | `docker-data/prometheus/prometheus.yml`           |
| Estado persistente de runtime                              | somente no servidor, em `/home/shared/docker-data`    | bancos, uploads, bibliotecas, caches persistentes |
| Arquivos `.env` e segredos                                 | repositório privado `server-FCT-DTI-X-01-secrets`     | `docker-compose/traefik/providers/crowdsec.yml`   |
| Estado local gerado por serviço                            | somente no servidor                                   | `acme.json` do Traefik                            |
| Projeto que precisa continuar como Git checkout            | `compose_external_repositories` + `compose-overlays/` | `unleash`                                         |

### Configuração pública

Coloque no repositório público tudo que pode ser lido sem credenciais e que
deve ser reproduzível:

- arquivos `docker-compose.yml`;
- providers sem tokens;
- configurações de Prometheus, Traefik, Grafana ou serviços equivalentes sem
  segredo;
- diretórios e arquivos vazios que precisam existir para bind mounts.

O role `compose` copia:

- `docker-compose/` para `/home/shared/docker-compose/`;
- `docker-data/` para `/home/shared/docker-data/`.

### Segredos e overlays privados

Arquivos sensíveis não entram neste repositório. Eles ficam no repositório
privado companheiro e são sobrepostos em `/home/shared/docker-compose/`.

O Ansible trata o checkout privado como autoritativo:

1. Verifica acesso ao repositório privado;
2. Clona ou atualiza `/home/shared/server-FCT-DTI-X-01-secrets`;
3. Força a versão configurada;
4. Roda `git clean -ffdx` para remover arquivos locais fora do Git;
5. Copia `server-FCT-DTI-X-01-secrets/docker-compose/` por cima de
   `/home/shared/docker-compose/`;
6. Remove overlays de segredo que ainda estavam implantados, mas não existem
   mais no Git privado.

Padrões tratados como segredos implantados:

- `.env`
- `*.env`
- `*.secret`
- `*.secret.*`
- `crowdsec.yml`

### Estado persistente

Não se deve fazer commit do estado de runtime. Ele fica no servidor, principalmente em
`/home/shared/docker-data`.

Declare apenas a existência esperada em
`host_vars/SECOMPP/60-compose-storage.yml`:

- `compose_data_directories`: diretórios persistentes que precisam existir;
- `compose_data_files`: arquivos persistentes que precisam existir.

O Ansible cria o que estiver faltando, mas não substitui dados existentes. Se um
bind mount precisar ser arquivo e no servidor houver um diretório no lugar, o
playbook falha para evitar perda de dados. Apenas diretórios vazios marcados com
`repair_empty_directory: true` são removidos e recriados como arquivo.

### Projetos Compose ativos

Um diretório em `docker-compose/` só é aplicado se estiver listado em
`compose_projects`, em `host_vars/SECOMPP/90-compose-projects.yml`.

Para adicionar um stack:

1. Crie `docker-compose/<projeto>/docker-compose.yml`;
2. Coloque segredos no repositório privado, não aqui;
3. Declare diretórios ou arquivos persistentes em `60-compose-storage.yml`;
4. Declare redes externas em `70-compose-networking.yml`, se necessário;
5. Adicione o nome do projeto em `compose_projects`;
6. Rode validação local ou aplique em uma janela de manutenção.

Para remover um stack:

1. Remova o projeto de `compose_projects`;
2. Se ele precisar ser limpo na transição, adicione o nome em
   `compose_retired_paths`;
3. Remova os arquivos públicos quando não forem mais referência;
4. Remova overlays privados correspondentes no repositório de segredos.

Projetos em `compose_retired_paths` têm containers e networks removidos pelo
label `com.docker.compose.project` antes de o diretório ser apagado.

## Validação antes de enviar mudanças

Valide a sintaxe Ansible:

```bash
ansible-playbook --syntax-check site.yml
```

Valide um Compose específico:

```bash
docker compose -f docker-compose/traefik/docker-compose.yml config --quiet
```

Depois que a mudança estiver no servidor, aplique manualmente se não quiser
esperar o timer:

```bash
sudo systemctl start ansible-pull-fct-dti-x-01.service
journalctl -u ansible-pull-fct-dti-x-01.service -n 200 --no-pager
```

## Operação diária

Fluxo recomendado:

1. Altere este repositório ou o repositório privado de segredos;
2. Faça commit e push;
3. No servidor, rode manualmente o service ou espere o timer;
4. Acompanhe o journal;
5. Verifique o serviço afetado com `docker compose ps` e logs do container.

Comandos úteis:

```bash
systemctl list-timers ansible-pull-fct-dti-x-01.timer
journalctl -u ansible-pull-fct-dti-x-01.service -f
docker ps
docker compose -f /home/shared/docker-compose/traefik/docker-compose.yml ps
```

## Referências

- [FCT-DTI-X-01](https://cacic.dev.br/docs/Recursos/Servidores/FCT-DTI-X-01)
- [Especificações comuns](https://cacic.dev.br/docs/Recursos/Servidores/Especifica%C3%A7%C3%B5es%20comuns)
- [Organização de arquivos](https://cacic.dev.br/docs/Recursos/Servidores/FCT-DTI-X-01/Organiza%C3%A7%C3%A3o%20de%20arquivos)
- [Firewall](https://cacic.dev.br/docs/Recursos/Servidores/FCT-DTI-X-01/Rede/Firewall)
- [Pacotes](https://cacic.dev.br/docs/Recursos/Servidores/FCT-DTI-X-01/HW%20e%20SW/Pacotes)
- [Sistema operacional](https://cacic.dev.br/docs/Recursos/Servidores/FCT-DTI-X-01/HW%20e%20SW/Sistema%20operacional)
