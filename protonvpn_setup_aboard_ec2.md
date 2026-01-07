# Setting up a VPN aboard an EC2 on AWS

## preliminary steps:
- Create a VPC (a block of address space inside the Amazon cloud)
- Create a subnet within that VPC (a possibly smaller block of address space than the VPC itself)
- For both the VPC and the subnet, enable IPv4 DNS address names
- Create an internet gateway (to manage communications between your VPC and the wider internet)
- Attach the internet gateway to the VPC
- Edit the VPC's routing table to allow traffic in and out of the gateway

## launch an EC2 instance:
- use the AWS EC2 dashboard to choose an EC2 with desired specifications
- configure the EC2 to belong to the VPC and subnet created above
- generate a private SSH key pair and download the .pem file to your local host
- launch the EC2 and take note of the IPv4 DNS address
- ssh into your EC2. For the Linux command line, it will be something like ssh -i "keypair.pem" ubuntu@EC2-public-DNS-address

## install Python on EC2:
ProtonVPN has a command-line interface (CLI) written in Python, so we will need to install Python first, and then the CLI tool, before proceeding.
- run "sudo apt-get update && sudo apt-get upgrade" to update the EC2's software
- as advised here https://github.com/pyenv/pyenv/wiki/Common-build-problems install dependencies as follows:
sudo apt-get install -y make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev python-openssl git
- the best way to manage Python versions is to install pyenv (see here https://realpython.com/python-virtual-environments-a-primer/). Install pyenv as follows:
'curl -L https://raw.githubusercontent.com/yyuu/pyenv-installer/master/bin/pyenv-installer | bash'. When this completes, three lines of code will appear in the terminal. Copy and paste these into the .bashrc file in your home directory, then save .bashrc and run it (execute 'source .bashrc'). The pyenv tool now works, and you can check what versions of python you have aboard the EC2 with the command 'pyenv versions'.
- Now install the latest version of python using pyenv. To do so, type 'pyenv install 3.9.' and then hit tab a few times to see the latest version.
- Once python is installed, set it to be the default python interpreter for the EC2 by typing 'pyenv global 3.9.?'

## install ProtonVPN CLI tool on EC2:
- As explained here (https://protonvpn.com/support/linux-vpn-tool/), you can install the ProtonVPN CLI by running 'sudo apt install -y openvpn dialog python3-pip python3-setuptools && sudo pip3 install protonvpn-cli'
- Initialize the CLI by running 'sudo protonvpn init'. Instead of using the username and password for accessing our account information, use the username and password for programmatic access, as described in the Account section of the protonvpn website. Also note that our subscription level is 'Plus'.
- Now that you have initialized the tool, it is ready for use. As soon as you mask your IP address, however, your local host will lose access to the EC2! To overcome this problem, we will use 'split tunneling', in which we make a specific exception whereby traffic from your IP address (but only your IP address!) is received to the EC2's public IP, while all other traffic in and out of the EC2 is routed via the proxy server. To configure split tunneling, type 'protonvpn configure' and then choose option 6. This will create a split_tunneling.txt file in your ProtonVPN home directory. Enable split tunneling and enter your local host's IP address. You can always add or remove IP addresses from this exceptions list.
- Refresh the tool with 'protonvpn refresh'
- Connect to a proxy server with any of the commands suggested in the above link. For example, to connect to the fastest server, type 'protonvpn c -f'.
