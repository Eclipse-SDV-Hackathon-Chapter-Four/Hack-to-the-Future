# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  config.vm.provider "virtualbox" do |vb|
    vb.memory = "8192"
    vb.cpus = 8
  end

  config.vm.provision "shell", inline: <<-SHELL
    apt-get update
    apt-get install -y docker.io docker-compose-v2
    usermod -aG docker vagrant  # allow running Docker commands without sudo (unsafe, but fine in VM)


    ##### Setup Rust toolchain #####
    apt-get install -y gcc

    setup_dir=/home/vagrant/.cache/setup
    mkdir -p $setup_dir
    chown vagrant $setup_dir

    echo "
      #!/bin/bash
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs > ~/.cache/setup/rustup.sh
      sh ~/.cache/setup/rustup.sh -y
    " > $setup_dir/setup_user.sh

    chmod +x $setup_dir/setup_user.sh
    su -l vagrant -c $setup_dir/setup_user.sh


    ##### Setup CAN for openDuT #####
    # https://opendut.eclipse.dev/book/user-manual/edgar/docker.html#can

    apt-get install -y linux-modules-extra-$(uname -r)  # vcan kernel module
    apt-get install -y can-utils  # candump, cansend, cangen

    echo "options can_gw max_hops=2" > /etc/modprobe.d/can.conf
    modprobe vcan
    modprobe can_gw
    echo -e "vcan\ncan_gw" | tee /etc/modules-load.d/can.conf
  SHELL
end
