#!/usr/bin/perl -w
#
# Petr Baudis (c) 2004, public domain
# Slightly inspired by Stefan "tommie" Tomanek's newsline.pl.
#
# RSS->IRC gateway

use strict;

use lib '/srv/home/pasky/perl/share/perl/5.8.4/';

$| = 1;


### Configuration section
# In our example setup, we are going to deliver Slashdot headlines.

use vars qw ($nick $server $port $channel $ircname @rss_url $refresh $extract_url $multisource);

#$server = 'irc.ipv6.cesnet.cz';
$server = 'open.ircnet.net';
#$server = 'irc.felk.cvut.cz';
#$server = 'irc.gts.sk';
$port = 6667;
#$port = 6697;

unless (@ARGV) {
  print STDERR "Usage: $0 <nick> <ircname> <channel> <refresh[minutes]> <rss_url1> <rss_url2> ...\n";
  print STDERR "The refresh can be prefixed by 'u' (show url= value instead) or 'a' (diverse sources).\n";
  exit;
}

#$nick = 'slashrss';
#$channel = '#linux.cz,#jikos';
#$rss_url = 'http://www.slashdot.org/slashdot.rss';
#$refresh = 30*60; # in seconds; Slashdot allows refresh max. once per 30 minutes
($nick, $ircname, $channel, $refresh, @rss_url) = @ARGV;
$extract_url = ($refresh =~ s/u//);
$multisource = ($refresh =~ s/a//);
$refresh *= 60;
$ircname .= ' (RSS feed)';



### Preamble

use POSIX;
use Net::IRC;
use LWP::UserAgent;
use XML::RSS;
use Cz::Cstocs;
use Encode;
use URI::Escape;



### Connection initialization

use vars qw ($irc $conn);

$irc = new Net::IRC;
$irc->timeout(300);
print "Connecting to server ".$server.":".$port." with nick ".$nick."...\n";
$conn = $irc->newconn (Nick => $nick, Server => $server, Port => $port,  # ssl => 1,
                       Ircname => $ircname);



### The event handlers

# Connect handler - we immediatelly try to join our channel.
sub on_connect {
  my ($self, $event) = @_;

  print "Joining channel ".$channel."...\n";
  $self->join ($channel);
}

$conn->add_handler ('welcome', \&on_connect);


# Joined the channel, so log that.
sub on_joined {
  my ($self, $event) = @_;

  print "Joined channel ".$channel."...\n";

  $SIG{ALRM} = \&check_all_rss;
  check_all_rss();
}

$conn->add_handler ('endofnames', \&on_joined);


# It is a good custom to reply to the CTCP VERSION request.
sub on_cversion {
  my ($self, $event) = @_;

  print "Got version query from ".$event->nick."\n";
  $self->ctcp_reply ($event->nick, 'VERSION RSS->IRS gateway IRC hack');
}

$conn->add_handler ('cversion', \&on_cversion);


# Let's make fun from newbies.
sub on_msg {
  my ($self, $event) = @_;

  my @args = $event->args;
  print "Got MSG from ".$event->nick.": @args\n" if @args;
  return unless (@args);
  if ($args[0] =~ s/^~msg\s+(\S+)\s+//) {
    $self->privmsg ($1, $args[0]);
    $self->privmsg ($event->nick, 'Sent to '.$1.': '.$args[0]);
  } elsif ($args[0] =~ s/^~refresh//) {
    alarm 0;
    check_all_rss();
  }
}

$conn->add_handler ('msg', \&on_msg);
#$conn->add_handler ('public', \&on_msg);



### The RSS feed

use vars qw (%items);


sub fetch_slashdot {
  my ($rss_url) = @_;
  my $ua = LWP::UserAgent->new (env_proxy => 1, keep_alive => 1, timeout => 30, agent => "curl/7.21.0");
  my $request = HTTP::Request->new('GET', "http://slashdot.org/");
  my @items = ();
  my $response = $ua->request ($request);
  return unless ($response->is_success);

  #print "[/.] Fetched HTML data:\n";
  for (split(/\n/, $response->content)) {
	  /\<span class="soda pop1".*\>([^\<]+:)\<\/a\>\s*\<a[\t ]*href="([^"]*)"[^>]*\>(.*)\<\/a\>.*/ or next;
	  my $item = { "title" => "$1 $3", "description" => "", "link" => "http:$2" };
	  $item->{title} =~ s/\<[^\>]+\>(.*?)<\/[^\>]+\>/\x1f$1\x1f/g;
	  push(@items, $item);

	  #print "-- Item: $item->{title} :: $item->{link}\n$item->{description}\n--\n\n";
  }

  return @items;
}


# Fetches the RSS from server and returns a list of RSS items.
sub fetch_rss {
  my ($rss_url) = @_;
  # agent: XXX politico bans libwww, wtf?!
  my $ua = LWP::UserAgent->new (env_proxy => 1, keep_alive => 1, timeout => 30, agent => "curl/7.21.0");
  my $request = HTTP::Request->new('GET', $rss_url);
  my $response = $ua->request ($request);
  unless ($response->is_success) {
    print STDERR "Error fetching $rss_url: $!\n";
    return ();
  }

#my $data = $response->decoded_content(charset => 'utf8');
my $data = $response->content();
  my $rss = new XML::RSS ();
  my $feed;
  $data =~ s/&nbsp;/ /g;
  eval { $feed = $rss->parse($data); };
  unless ($feed) {
    print STDERR "Error parsing $rss_url: $!\n";
    return ();
  }

#print "[$rss_url] Fetched XML data, encoding ".$rss->encoding.":\n";
  my $conv = new Cz::Cstocs 'utf8', 'ascii';

  foreach my $item (@{$rss->{items}}) {
    # Convert to something reasonable.
    $item->{title} = encode('utf8', $item->{title}); # if (uc($rss->encoding) eq 'UTF-8');
    $item->{title} = &$conv($item->{title});
    # Make sure to strip any possible newlines and similiar stuff.
    $item->{title} =~ s/\s/ /g;
    $item->{title} =~ s/^ *//;
    $item->{title} =~ s/ *$//;
    $item->{link} =~ s/\s/ /g;
    $item->{link} =~ s/^ *//;
    $item->{link} =~ s/ *$//;

    $item->{description} = encode('utf8', $item->{description}); # if (uc($rss->encoding) eq 'UTF-8');
    $item->{description} = &$conv($item->{description});

    #print "-- Item: $item->{title} :: $item->{link}\n$item->{description}\n--\n\n";

    if ($nick eq 'slashdot' and $item->{link} =~ /feeds\.feedburner/) {
      $conn->privmsg ($channel, "\2/. kravi :/\3");
      return ();
    }
  }

  return @{$rss->{items}};
}


# Attempts to find some newly appeared RSS items.
# Now it just reports all the items present in the new and not presented
# in the old, since some servers don't keep this proeprly sorted.
sub delta_rss {
  my ($seen, $new) = @_;
  my $empty = !(keys %$seen);

  my @delta = grep { not exists $seen->{$_->{title}}; } @$new;
  foreach (@delta) {
    $seen->{$_->{title}} = 1;
    #print "<".$_->{title}.">\n";
  }

  # If %$seen was empty, it means this is the first run and we will therefore not
  # return anything.
  return $empty ? () : @delta;
}


# Check RSS feed periodically.
sub check_rss {
  my ($rss_url) = @_;
  my ($rss_name, @new_items);

  if ($multisource) {
    $rss_url =~ s/^(.*)\|//;
    $rss_name = "[".$1."] ";
  } else {
    $rss_name = '';
  }

#if ($nick eq 'slashdot') {
#       @new_items = fetch_slashdot();
# } else {
  	@new_items = fetch_rss ($rss_url);
# }
  if (@new_items) {
    $items{$rss_url} ||= {};
    my @delta = delta_rss ($items{$rss_url}, \@new_items);
    foreach my $item (reverse @delta) {
      if ($extract_url) {
	my $link = $item->{link};
	if ($link =~ s/^.*[?&]url=//) { # XXX: We rely on it being the last parameter.
	  if ($link !~ /^http/) {
	    $link = 'http:' . $link
	  }
	  $item->{link} = uri_unescape($link);
	}
      }
      $conn->privmsg ($channel, $rss_name . "\2" . $item->{title}."\2 \3"."14::\3 ".$item->{link});
      #print ("New: " . $rss_name . $item->{title}." :: ".$item->{link});
    }
  }
}

sub check_all_rss {
  foreach my $rss_url (@rss_url) {
    check_rss($rss_url);
    sleep 1;
  }

  alarm $refresh;
}


# Fire up the IRC loop.
$irc->start;
