#!/bin/bash
set -eu

die() { echo "$@"; exit 1; }

# Check system Python version
SYSTEM_PYTHON_VERSION=$(echo "$(python -c 'import sys; print(sys.version_info[0])').$(python -c 'import sys; print(sys.version_info[1])')" | bc -l)
if [ "$(echo "$SYSTEM_PYTHON_VERSION > 3" | bc)" -eq 1 ]; then
  die "Your system Python version is $SYSTEM_PYTHON_VERSION. This Re:dash setup script is available on Python 2.7."
elif [ "$(echo "$SYSTEM_PYTHON_VERSION < 2.7" | bc)" -eq 1 ]; then
  die "Your system Python version is old. This Re:dash setup script is available on Python 2.7."
else
  echo "Your system Python version is $SYSTEM_PYTHON_VERSION. Re:dash setup starting."
fi


REDASH_BASE_PATH=/usr/local/opt/redash
REDASH_CLONE_DIR=$(cd $(dirname $BASH_SOURCE) && pwd)/../..
FILES_BASE_DIR=$REDASH_CLONE_DIR/setup/mac/files

# Default branch/version to master if not specified in REDASH_BRANCH env var
REDASH_BRANCH="${REDASH_BRANCH:-master}"

# Base packages
brew update
brew install nginx wget pwgen node
# BigQuery dependencies:
brew install libffi
# MySQL dependencies:
brew install mysql
# Microsoft SQL Server dependencies:
brew install homebrew/versions/freetds091
#Saml dependency
brew install libxmlsec1

pip install -U setuptools==23.1.0

# Install requiements
cd $REDASH_CLONE_DIR
pip install -r requirements.txt

# Init env file
cp .env.example .env

# PostgreSQL
pg_available=0
psql --version || pg_available=$?
if [ $pg_available -ne 0 ]; then
    brew install postgresql
fi

# Starting Postgres
PGDATA=/usr/local/var/postgres
export PGDATA=$PGDATA
pg_ctl start -l $PGDATA/server.log



# Redis
redis_available=0
redis-cli --version || redis_available=$?
if [ $redis_available -ne 0 ]; then
	brew install redis
fi
# Setup configuration
REDIS_PORT=6379
REDIS_CONFIG_FILE="/usr/local/etc/redis/$REDIS_PORT.conf"
REDIS_LOG_FILE="/usr/local/var/log/redis_$REDIS_PORT.log"
REDIS_DATA_DIR="/usr/local/var/lib/redis/$REDIS_PORT"

mkdir -p "$(dirname "$REDIS_CONFIG_FILE")" || die "Could not create redis config directory"
mkdir -p "$(dirname "$REDIS_LOG_FILE")" || die "Could not create redis log dir"
mkdir -p "$REDIS_DATA_DIR" || die "Could not create redis data directory"

cp $FILES_BASE_DIR/"redis.conf" $REDIS_CONFIG_FILE 
redis-server $REDIS_CONFIG_FILE

# Directories
if [ ! -d "$REDASH_BASE_PATH" ]; then
    mkdir $REDASH_BASE_PATH
    mkdir $REDASH_BASE_PATH/logs
fi


# Create database / tables
pg_user_exists=0
echo "Next create redash postgres user & database."
psql postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='redash'" | grep -q 1 || pg_user_exists=$?
if [ $pg_user_exists -ne 0 ]; then
    echo "Creating redash postgres user & database."
    createuser redash --no-superuser --no-createdb --no-createrole
    createdb redash --owner=redash

    cd $REDASH_CLONE_DIR
    bin/run ./manage.py database create_tables
fi

# Create default admin user
cd $REDASH_CLONE_DIR
echo "Next create redash admin user."
bin/run ./manage.py users create --admin --password admin "Admin" "admin"


# Create re:dash read only pg user & setup data source
pg_user_exists=0
psql postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='redash_reader'" | grep -q 1 || pg_user_exists=$?
if [ $pg_user_exists -ne 0 ]; then
    echo "Creating redash reader postgres user."
    REDASH_READER_PASSWORD=$(pwgen -1)
    psql -d redash -c "CREATE ROLE redash_reader WITH PASSWORD '$REDASH_READER_PASSWORD' NOCREATEROLE NOCREATEDB NOSUPERUSER LOGIN"
    psql -d redash -c "grant select(id,name,type) ON data_sources to redash_reader;" redash
    psql -d redash -c "grant select(id,name) ON users to redash_reader;" redash
    psql -d redash -c "grant select on alerts, alert_subscriptions, groups, events, queries, dashboards, widgets, visualizations, query_results to redash_reader;" redash

    cd $REDASH_CLONE_DIR
    bin/run ./manage.py ds new "Re:dash Metadata" --type "pg" --options "{\"user\": \"redash_reader\", \"password\": \"$REDASH_READER_PASSWORD\", \"host\": \"localhost\", \"dbname\": \"redash\"}"
fi

# Pip requirements for all data source types
cd $REDASH_CLONE_DIR
pip install -r requirements_all_ds.txt

# Nginx setup
mkdir -p /usr/local/etc/nginx/sites-available
mkdir -p /usr/local/etc/nginx/sites-enabled
NGINX_CONF_DIR=/usr/local/etc/nginx
cp $FILES_BASE_DIR/"nginx_redash_site" /usr/local/etc/nginx/sites-available/redash
cp $FILES_BASE_DIR/"nginx_redash_on_mac.conf" $NGINX_CONF_DIR/nginx_redash_on_mac.conf
ln -nfs /usr/local/etc/nginx/sites-available/redash /usr/local/etc/nginx/sites-enabled/redash
NGINX_PIDFILE=/usr/local/var/run/nginx.pid
if [ ! -f $NGINX_PIDFILE ]
then
    echo "$NGINX_PIDFILE does not exist, process is not running"
    echo "nginx start"
    nginx -c $NGINX_CONF_DIR/nginx_redash_on_mac.conf
else
    PID=$(cat $NGINX_PIDFILE)
    echo "Stopping ..."
    nginx -s stop
    while [ -x $NGINX_PIDFILE ]
    do
        echo "Waiting for Nginx to shutdown ..."
        sleep 1
    done
    echo "nginx stopped"
    echo "nginx restart"
    nginx -c $NGINX_CONF_DIR/nginx_redash_on_mac.conf
fi

# Build frontend
echo "Buliding frontend."
make

# Setup supervisord
mkdir -p $REDASH_BASE_PATH/supervisord
pip install supervisor==3.1.2 # TODO: move to requirements.txt

# Use GNU sed
brew install gnu-sed
# Setup supervisord config file
SUPERVISORD_CONF_PATH=$REDASH_BASE_PATH/supervisord/supervisord.conf
cp $FILES_BASE_DIR/"supervisord.conf" $SUPERVISORD_CONF_PATH 
gsed -i -e '/^directory=/d' $SUPERVISORD_CONF_PATH
gsed -i -e '/^command=/d' $SUPERVISORD_CONF_PATH
gsed -i -e '/^pidfile/ a directory='$REDASH_CLONE_DIR $SUPERVISORD_CONF_PATH
gsed -i -e '/^\[program:redash_server\]$/ a command='$REDASH_CLONE_DIR'/bin/run gunicorn -b 127.0.0.1:5001 --name redash -w 4 --max-requests 1000 redash.wsgi:app' $SUPERVISORD_CONF_PATH
gsed -i -e '/^\[program:redash_celery\]$/ a command='$REDASH_CLONE_DIR'/bin/run celery worker --app=redash.worker --beat -c2 -Qqueries,celery --maxtasksperchild=10 -Ofair' $SUPERVISORD_CONF_PATH
gsed -i -e '/^\[program:redash_celery_scheduled\]$/ a command='$REDASH_CLONE_DIR'/bin/run celery worker --app=redash.worker -c2 -Qscheduled_queries --maxtasksperchild=10 -Ofair' $SUPERVISORD_CONF_PATH

# Start redash
echo "Start redash"
cd $REDASH_CLONE_DIR
SUPERVISORD_PIDFILE=$REDASH_BASE_PATH/supervisord/supervisord.pid
if [ ! -f $SUPERVISORD_PIDFILE ]
then
    echo "supervisor start"
    supervisord -c $SUPERVISORD_CONF_PATH
else
    PID=$(cat $SUPERVISORD_PIDFILE)
    echo "supervisorctl update"
    supervisorctl -c $SUPERVISORD_CONF_PATH update
fi

echo "Check process status"
$FILES_BASE_DIR/redash_init status
if [ -f $SUPERVISORD_PIDFILE ]
then
    sleep 2 &
    wait $!
    supervisorctl status all
fi
echo "Setup finished"
